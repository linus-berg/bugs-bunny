"""HTTP API."""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Iterable
from typing import IO, Annotated, Any, Literal

from fastapi import APIRouter, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__, extraction
from app.config import Settings
from app.errors import ServiceError
from app.pages import PageRanges, parse_page_spec
from app.pool import WorkerPool

log = logging.getLogger("app")

OCR_LANGUAGE_PATTERN = r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$"
MULTIPART_OVERHEAD = 1024 * 1024
"""Extra request-body bytes allowed beyond the file limit, for multipart framing and form fields."""

PUBLIC_PATHS = frozenset({"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


# --------------------------------------------------------------------------
# Request parameters
# --------------------------------------------------------------------------


class ExtractionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo like `page=` should fail loudly, not be ignored

    pages: str | None = Field(
        None,
        max_length=1000,
        description="Pages to process, 1-based and inclusive: `1-3,5,10-` (`10-` = page 10 to the end). Default: all.",
        examples=["1-3,5"],
    )
    ocr: Literal["auto", "off", "force"] = Field(
        "auto",
        description="`auto` OCRs only pages without usable text (e.g. scans), `force` OCRs every page, `off` never OCRs.",
    )
    ocr_language: str | None = Field(
        None,
        max_length=200,
        pattern=OCR_LANGUAGE_PATTERN,
        description="Tesseract language(s) for OCR, e.g. `eng` or `eng+deu`. Default: the server's `OCR_LANGUAGE`.",
    )
    tables: Literal["markdown", "html"] = Field(
        "markdown",
        description="Render tables as Markdown or as HTML (better for merged cells). Ignored for plain text.",
    )

    @field_validator("pages")
    @classmethod
    def _validate_pages(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parse_page_spec(value)
        return value

    @property
    def page_ranges(self) -> PageRanges | None:
        return parse_page_spec(self.pages) if self.pages else None


class TextParams(ExtractionParams):
    format: Literal["markdown", "text"] = Field("markdown", description="Output format.")
    page_headers: bool = Field(True, description="Include running page headers (e.g. a title repeated on each page).")
    page_footers: bool = Field(True, description="Include running page footers (e.g. page numbers).")


class ChunkParams(ExtractionParams):
    max_tokens: int = Field(400, ge=1, le=100_000, description="Target maximum chunk size in tokens (approximate).")
    min_tokens: int = Field(120, ge=0, le=100_000, description="Chunks smaller than this are merged with a neighbour.")
    table_mode: Literal["preserve", "isolate"] = Field(
        "preserve", description="`isolate` puts every table in a chunk of its own."
    )
    headers_footers: Literal["exclude", "auto", "include"] = Field(
        "exclude",
        description="Page headers/footers: drop those the layout model detects, drop those repeated across pages (`auto`), or keep all.",
    )
    tagged: bool = Field(False, description="Add `tagged_content`: the chunk text prefixed with its section, page and type, for embedding.")

    @model_validator(mode="after")
    def _validate_tokens(self) -> ChunkParams:
        if self.min_tokens > self.max_tokens:
            raise ValueError("min_tokens must not exceed max_tokens")
        return self


DocumentFile = Annotated[
    UploadFile,
    File(description="A PDF. XPS, EPUB, MOBI, FB2, CBZ, SVG and images (PNG, JPEG, TIFF, ...) work too; images are OCRed."),
]
Password = Annotated[
    str | None,
    Form(description="Password for an encrypted document. A form field, so it stays out of URLs and access logs."),
]


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    detail: Any = Field(description="Human-readable message (a list of problems for `invalid_request`).")
    code: str = Field(description="Stable machine-readable error code.")


class TocEntry(BaseModel):
    level: int
    title: str
    page: int | None


class FormField(BaseModel):
    name: str
    value: Any
    pages: list[int]


class DocumentInfo(BaseModel):
    filename: str | None
    format: str | None = Field(description="Detected file format, e.g. `PDF 1.7`.")
    page_count: int
    encrypted: bool
    title: str | None
    author: str | None
    subject: str | None
    keywords: str | None
    creator: str | None
    producer: str | None
    creation_date: str | None = Field(description="ISO 8601 when parseable, otherwise as stored in the file.")
    modification_date: str | None
    toc: list[TocEntry] = Field(description="The document outline (bookmarks).")
    form_fields: list[FormField] = Field(description="Values of fillable PDF form fields.")


class PageContent(BaseModel):
    page: int = Field(description="1-based page number.")
    content: str


class ExtractResponse(BaseModel):
    document: DocumentInfo
    format: Literal["markdown", "text"]
    pages: list[PageContent]


class Chunk(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    text: str
    content_hash: str | None = None
    metadata: dict[str, Any] = Field(
        description="Pages, bounding boxes, section path, token count, element types, OCR flag, ..."
    )
    tagged_content: str | None = None


class ChunksResponse(BaseModel):
    document: DocumentInfo
    chunks: list[Chunk]


class OcrInfo(BaseModel):
    available: bool
    languages: list[str] | None = Field(description="Installed Tesseract languages; null if the engine does not say.")
    default_language: str


class Limits(BaseModel):
    max_upload_mb: float
    max_pages: int | None = Field(description="Per request; null means unlimited.")
    processing_timeout_seconds: float


class ServiceInfo(BaseModel):
    version: str
    workers: int
    limits: Limits
    ocr: OcrInfo
    libraries: dict[str, str]


class WorkerStats(BaseModel):
    size: int
    idle: int
    queued: int


class Health(BaseModel):
    status: Literal["ok"]
    workers: WorkerStats | None


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: {"model": ErrorResponse} for code in codes}


DOCUMENT_ERRORS = _errors(400, 401, 413, 422, 500, 503, 504)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

router = APIRouter()


@router.post(
    "/convert",
    response_class=PlainTextResponse,
    responses={200: {"content": {"text/plain": {}}}, **DOCUMENT_ERRORS},
    summary="Extract the text of a document as Markdown or plain text",
)
async def convert(
    request: Request,
    file: DocumentFile,
    params: Annotated[TextParams, Query()],
    password: Password = None,
) -> PlainTextResponse:
    text = await _process(request, file, extraction.convert, password, **_text_options(request, params, per_page=False))
    return PlainTextResponse(text)


@router.post(
    "/extract",
    response_model=ExtractResponse,
    responses=DOCUMENT_ERRORS,
    summary="Extract text page by page, together with document metadata",
)
async def extract(
    request: Request,
    file: DocumentFile,
    params: Annotated[TextParams, Query()],
    password: Password = None,
) -> dict[str, Any]:
    result = await _process(request, file, extraction.convert, password, **_text_options(request, params, per_page=True))
    return {
        "document": {"filename": file.filename, **result["document"]},
        "format": params.format,
        "pages": result["pages"],
    }


@router.post(
    "/chunks",
    response_model=ChunksResponse,
    responses=DOCUMENT_ERRORS,
    summary="Split a document into layout-aware chunks for retrieval (RAG)",
)
async def chunks(
    request: Request,
    file: DocumentFile,
    params: Annotated[ChunkParams, Query()],
    password: Password = None,
) -> dict[str, Any]:
    result = await _process(
        request,
        file,
        extraction.chunk,
        password,
        pages=params.page_ranges,
        max_pages=request.app.state.settings.max_pages,
        tables=params.tables,
        max_tokens=params.max_tokens,
        min_tokens=params.min_tokens,
        table_mode=params.table_mode,
        headers_footers=params.headers_footers,
        tagged=params.tagged,
        **_ocr_options(request, params),
    )
    return {"document": {"filename": file.filename, **result["document"]}, "chunks": result["chunks"]}


@router.post(
    "/metadata",
    response_model=DocumentInfo,
    responses=DOCUMENT_ERRORS,
    summary="Read document metadata, outline and form fields (fast, no text extraction)",
)
async def metadata(request: Request, file: DocumentFile, password: Password = None) -> dict[str, Any]:
    info = await _process(request, file, extraction.document_info, password)
    return {"filename": file.filename, **info}


@router.get("/info", response_model=ServiceInfo, responses=_errors(401), summary="Server limits and capabilities")
async def info(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    capabilities = request.app.state.pool.capabilities
    return {
        "version": __version__,
        "workers": settings.workers,
        "limits": {
            "max_upload_mb": settings.max_upload_mb,
            "max_pages": settings.max_pages or None,
            "processing_timeout_seconds": settings.processing_timeout,
        },
        "ocr": {
            "available": capabilities.get("ocr_available", False),
            "languages": capabilities.get("ocr_languages"),
            "default_language": settings.ocr_language,
        },
        "libraries": capabilities.get("versions", {}),
    }


@router.get("/health", response_model=Health, summary="Liveness check")
async def health(request: Request) -> dict[str, Any]:
    pool: WorkerPool | None = getattr(request.app.state, "pool", None)
    return {"status": "ok", "workers": pool.stats if pool else None}


# --------------------------------------------------------------------------
# Request processing
# --------------------------------------------------------------------------


def _text_options(request: Request, params: TextParams, *, per_page: bool) -> dict[str, Any]:
    return {
        "output": params.format,
        "pages": params.page_ranges,
        "max_pages": request.app.state.settings.max_pages,
        "page_headers": params.page_headers,
        "page_footers": params.page_footers,
        "tables": params.tables,
        "per_page": per_page,
        **_ocr_options(request, params),
    }


def _ocr_options(request: Request, params: ExtractionParams) -> dict[str, Any]:
    """Validate OCR options against what the workers support, before any work is done."""
    settings: Settings = request.app.state.settings
    capabilities = request.app.state.pool.capabilities
    language = params.ocr_language or settings.ocr_language
    available = capabilities.get("ocr_available", False)

    if params.ocr == "force" and not available:
        raise ServiceError(422, "ocr_unavailable", "OCR was requested but no OCR engine is installed on this server.")
    if params.ocr != "off" and available:
        missing = _missing_languages(language, capabilities.get("ocr_languages"))
        if missing:
            installed = ", ".join(capabilities["ocr_languages"]) or "none"
            raise ServiceError(
                422,
                "unsupported_ocr_language",
                f"OCR language(s) not installed: {', '.join(missing)}. Installed: {installed}.",
            )
    return {"ocr": params.ocr, "ocr_language": language}


def _missing_languages(language: str, installed: Iterable[str] | None) -> list[str]:
    if installed is None:  # the engine doesn't report languages; let it decide
        return []
    installed = set(installed)
    return [lang for lang in language.split("+") if lang not in installed]


async def _process(request: Request, file: UploadFile, fn: Callable, password: str | None, **options: Any) -> Any:
    state = request.app.state
    settings: Settings = state.settings
    started = time.perf_counter()
    outcome = "error"
    try:
        async with _saved_upload(file, settings.max_upload_bytes) as path:
            result = await state.pool.run(
                fn, timeout=settings.processing_timeout, path=path, password=password or None, **options
            )
        outcome = "ok"
        return result
    except ServiceError as e:
        outcome = e.code
        raise
    finally:
        log.info(
            "%s bytes=%s outcome=%s duration_ms=%.0f",
            request.url.path,
            file.size,
            outcome,
            (time.perf_counter() - started) * 1000,
        )


@contextlib.asynccontextmanager
async def _saved_upload(file: UploadFile, max_bytes: int) -> AsyncIterator[str]:
    """Copy the upload to a private temp file that worker processes can read."""
    if file.size is not None and file.size > max_bytes:
        raise _too_large(max_bytes)
    fd, path = tempfile.mkstemp(prefix="upload-")
    try:
        with os.fdopen(fd, "wb") as out:
            size = await run_in_threadpool(_copy_limited, file.file, out, max_bytes)
        if size == 0:
            raise ServiceError(400, "empty_file", "The uploaded file is empty.")
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _copy_limited(src: IO[bytes], dst: IO[bytes], max_bytes: int) -> int:
    src.seek(0)
    total = 0
    while chunk := src.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise _too_large(max_bytes)
        dst.write(chunk)
    return total


def _too_large(max_bytes: int) -> ServiceError:
    return ServiceError(
        413, "file_too_large", f"The upload exceeds this server's limit of {max_bytes / (1024 * 1024):g} MiB."
    )


# --------------------------------------------------------------------------
# Middleware and error handling
# --------------------------------------------------------------------------


def _error_response(exc: ServiceError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status_code, headers=exc.headers)


def _app_path(scope: Scope) -> str:
    path, root = scope["path"], scope.get("root_path", "")
    return path[len(root):] if root and path.startswith(root) else path


class APIKeyMiddleware:
    """Require an API key (``X-API-Key`` or ``Authorization: Bearer``) when keys are configured.

    A middleware rather than a FastAPI dependency because dependencies run
    only after the request body has been read; this rejects unauthenticated
    clients before they can upload anything.
    """

    def __init__(self, app: ASGIApp, keys: Iterable[str]) -> None:
        self.app = app
        self.keys = [k.encode() for k in keys]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.keys or _app_path(scope) in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        supplied = headers.get("x-api-key", "").strip()
        if not supplied:
            scheme, _, token = headers.get("authorization", "").partition(" ")
            supplied = token.strip() if scheme.lower() == "bearer" else None
        if supplied and any(secrets.compare_digest(supplied.encode(), key) for key in self.keys):
            await self.app(scope, receive, send)
            return
        error = ServiceError(401, "unauthorized", "Missing or invalid API key.", headers={"WWW-Authenticate": "Bearer"})
        await _error_response(error)(scope, receive, send)


class BodySizeLimitMiddleware:
    """Reject oversized request bodies while they stream in, not after buffering them to disk."""

    def __init__(self, app: ASGIApp, max_body_bytes: int, max_file_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_file_bytes = max_file_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        content_length = Headers(scope=scope).get("content-length", "")
        if content_length.isdigit() and int(content_length) > self.max_body_bytes:
            await _error_response(_too_large(self.max_file_bytes))(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # Raised inside the route's body parsing, so FastAPI passes
                    # it through to our ServiceError handler.
                    raise _too_large(self.max_file_bytes)
            return message

        await self.app(scope, limited_receive, send)


async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    return _error_response(exc)


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # `ctx` holds the raw exception objects, which serialise to `{}`; `msg` already says it all.
    errors = [{k: v for k, v in error.items() if k != "ctx"} for error in exc.errors()]
    return JSONResponse({"detail": jsonable_encoder(errors), "code": "invalid_request"}, status_code=422)


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error on %s", request.url.path, exc_info=exc)
    return JSONResponse({"detail": "Internal server error.", "code": "internal_error"}, status_code=500)


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

DESCRIPTION = """
Extract text from PDFs (and other documents) for search indexing, LLMs and RAG pipelines.

* **`/convert`** returns the text as Markdown or plain text.
* **`/extract`** returns the text page by page together with metadata.
* **`/chunks`** splits the document into retrieval-ready chunks with page and section references.
* **`/metadata`** reads metadata, outline and form fields without extracting text.

Scanned pages are OCRed automatically when an OCR engine is installed. Encrypted
PDFs are supported via the `password` form field. Errors are JSON objects with a
human-readable `detail` and a stable `code`.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
        pool = WorkerPool(
            settings.workers,
            initializer=extraction.initialize,
            init_kwargs={"onnx_threads": settings.onnx_threads},
            max_tasks_per_worker=settings.max_tasks_per_worker,
            max_queue=settings.max_queue,
        )
        await pool.start()
        try:
            _check_default_language(settings, pool.capabilities)
            app.state.pool = pool
            yield
        finally:
            await pool.close()

    app = FastAPI(
        title="PDF Text Extraction Service",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router)
    app.add_exception_handler(ServiceError, _service_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
    # The last middleware added runs first: authenticate before reading any body.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD,
        max_file_bytes=settings.max_upload_bytes,
    )
    app.add_middleware(APIKeyMiddleware, keys=settings.api_keys)
    return app


def _check_default_language(settings: Settings, capabilities: dict[str, Any]) -> None:
    if not capabilities.get("ocr_available"):
        log.warning("No OCR engine found; scanned pages and images will yield no text.")
        return
    missing = _missing_languages(settings.ocr_language, capabilities.get("ocr_languages"))
    if missing:
        raise RuntimeError(
            f"OCR_LANGUAGE={settings.ocr_language!r} is not installed (missing: {', '.join(missing)}). "
            f"Installed: {', '.join(capabilities.get('ocr_languages') or []) or 'none'}."
        )


app = create_app()
