"""Document processing on top of pymupdf4llm.

Everything here runs inside worker processes (see ``pool.py``), never in the
web server process: PyMuPDF is not thread-safe, the layout analysis is
CPU-bound, and malformed documents can hang or crash MuPDF. Functions take
and return plain picklable values and signal client-facing failures by
raising :class:`ServiceError`.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import pymupdf
import pymupdf4llm
from pymupdf4llm.helpers import document_layout
from pymupdf4llm.ocr import OCRMode

from app.errors import ServiceError
from app.pages import PageRanges, resolve_pages

log = logging.getLogger(__name__)

OcrMode = Literal["auto", "off", "force"]
TableFormat = Literal["markdown", "html"]

# Resolved once per worker by initialize(). Without this the library probes
# for OCR engines on every call, which spawns `tesseract`/`whereis`
# subprocesses when TESSDATA_PREFIX is unset.
_ocr_function = None
_ocr_languages: tuple[str, ...] | None = None  # None: engine does not report languages
_onnx_limited = False


def initialize(onnx_threads: int = 1) -> dict[str, Any]:
    """One-time worker setup; returns the capabilities this worker supports."""
    global _ocr_function, _ocr_languages
    # Library chatter ("OCR on page 3", ...) goes to logging, not raw stdout.
    pymupdf.set_messages(pylogging_name="pymupdf", pylogging_level=logging.DEBUG)
    if onnx_threads > 0:
        _limit_onnx_threads(onnx_threads)
    _ocr_function = document_layout.select_ocr_function()
    _ocr_languages = _tesseract_languages() if _ocr_function else ()
    _warm_up()
    return {
        "ocr_available": _ocr_function is not None,
        "ocr_languages": None if _ocr_languages is None else list(_ocr_languages),
        "versions": {"pymupdf": pymupdf.__version__, "pymupdf4llm": pymupdf4llm.__version__},
    }


def document_info(*, path: str, password: str | None = None) -> dict[str, Any]:
    """Metadata, outline and form fields. Cheap: no layout analysis."""
    with _open_document(path, password) as doc:
        return _document_info(doc)


def convert(
    *,
    path: str,
    password: str | None = None,
    output: Literal["markdown", "text"] = "markdown",
    pages: PageRanges | None = None,
    max_pages: int = 0,
    ocr: OcrMode = "auto",
    ocr_language: str = "eng",
    page_headers: bool = True,
    page_footers: bool = True,
    tables: TableFormat = "markdown",
    per_page: bool = False,
) -> str | dict[str, Any]:
    """Extract text as Markdown or plain text.

    Returns the full text, or with ``per_page`` a dict holding document info
    and a list of ``{"page": n, "content": ...}`` entries.
    """
    with _open_document(path, password) as doc:
        info = _document_info(doc) if per_page else None
        options: dict[str, Any] = {
            "pages": _select_pages(doc, pages, max_pages),
            "header": page_headers,
            "footer": page_footers,
            "page_chunks": per_page,
            **_ocr_options(ocr, ocr_language),
        }
        if output == "markdown":
            if tables == "html":
                options["table_output"] = "html"
            result = pymupdf4llm.to_markdown(doc, **options)
        else:
            result = pymupdf4llm.to_text(doc, **options)

    if not per_page:
        return result
    return {
        "document": info,
        "pages": [{"page": c["metadata"]["page_number"], "content": c["text"]} for c in result],
    }


def chunk(
    *,
    path: str,
    password: str | None = None,
    pages: PageRanges | None = None,
    max_pages: int = 0,
    ocr: OcrMode = "auto",
    ocr_language: str = "eng",
    tables: TableFormat = "markdown",
    max_tokens: int = 400,
    min_tokens: int = 120,
    table_mode: Literal["preserve", "isolate"] = "preserve",
    headers_footers: Literal["exclude", "auto", "include"] = "exclude",
    tagged: bool = False,
) -> dict[str, Any]:
    """Split the document into layout-aware chunks for retrieval (RAG)."""
    with _open_document(path, password) as doc:
        info = _document_info(doc)
        chunked = pymupdf4llm.to_chunks(
            doc,
            pages=_select_pages(doc, pages, max_pages),
            force_text=True,  # match to_markdown's default
            table_output=tables,
            max_tokens=max_tokens,
            min_tokens=min_tokens,
            table_mode=table_mode,
            header_footer_mode=headers_footers,
            **_ocr_options(ocr, ocr_language),
        )
        chunks = chunked.to_dicts(include_tagged=tagged)

    for c in chunks:
        c.get("metadata", {}).pop("file_path", None)  # always empty: documents are opened from memory
    return {"document": info, "chunks": chunks}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _open_document(path: str, password: str | None) -> Iterator[pymupdf.Document]:
    # Open from memory rather than from the path so that the server-side temp
    # file name never ends up in any output (pymupdf4llm echoes doc.name).
    with open(path, "rb") as f:
        data = f.read()
    try:
        # No filetype hint: MuPDF sniffs the content, so a mislabelled file
        # (e.g. a PNG uploaded as "scan.pdf") is still handled correctly.
        doc = pymupdf.open(stream=data)
    except pymupdf.EmptyFileError:
        raise ServiceError(400, "empty_file", "The uploaded file is empty.") from None
    except Exception:
        raise ServiceError(
            422,
            "invalid_document",
            "The file could not be opened. It is not a valid PDF or other supported document.",
        ) from None

    try:
        if doc.needs_pass:
            if not password:
                raise ServiceError(
                    422,
                    "password_required",
                    "The document is encrypted. Provide its password in the 'password' form field.",
                )
            if not doc.authenticate(password):
                raise ServiceError(422, "invalid_password", "The supplied password is incorrect.")
        if doc.page_count == 0:
            raise ServiceError(422, "invalid_document", "The document has no pages.")
        yield doc
    finally:
        # pymupdf4llm closes the caller's document itself when it converts an
        # image to PDF for OCR, and closing twice raises.
        if not doc.is_closed:
            doc.close()
        _release_memory()


def _select_pages(doc: pymupdf.Document, ranges: PageRanges | None, max_pages: int) -> list[int] | None:
    if ranges:
        try:
            selected: list[int] | None = resolve_pages(ranges, doc.page_count)
        except ValueError as e:
            raise ServiceError(422, "invalid_page_range", str(e)) from None
        count = len(selected)
    else:
        selected, count = None, doc.page_count

    if max_pages and count > max_pages:
        raise ServiceError(
            422,
            "too_many_pages",
            f"{count} pages selected but this server processes at most {max_pages} per request. "
            "Use the 'pages' parameter to process the document in parts.",
        )
    return selected


def _ocr_options(mode: OcrMode, language: str) -> dict[str, Any]:
    if mode == "off" or (mode == "auto" and _ocr_function is None):
        return {"use_ocr": OCRMode.NEVER}
    if _ocr_function is None:
        raise ServiceError(422, "ocr_unavailable", "OCR was requested but no OCR engine is installed on this server.")
    options: dict[str, Any] = {
        # Only OCR pages that need it, and keep text layers that previous OCR
        # already produced instead of redoing the work.
        "use_ocr": OCRMode.SELECT_KEEP_OLD,
        "ocr_language": language,
        "ocr_function": _ocr_function,
    }
    if mode == "force":
        options["force_ocr"] = True
    return options


def _document_info(doc: pymupdf.Document) -> dict[str, Any]:
    meta = doc.metadata or {}

    def text(key: str) -> str | None:
        return (meta.get(key) or "").strip() or None

    return {
        "format": text("format"),
        "page_count": doc.page_count,
        "encrypted": bool(meta.get("encryption")),
        "title": text("title"),
        "author": text("author"),
        "subject": text("subject"),
        "keywords": text("keywords"),
        "creator": text("creator"),
        "producer": text("producer"),
        "creation_date": parse_pdf_date(meta.get("creationDate")),
        "modification_date": parse_pdf_date(meta.get("modDate")),
        "toc": [
            {"level": level, "title": title, "page": page if page > 0 else None}
            for level, title, page in doc.get_toc(simple=True)
        ],
        "form_fields": _form_fields(doc),
    }


def _form_fields(doc: pymupdf.Document) -> list[dict[str, Any]]:
    if not doc.is_pdf or not doc.is_form_pdf:
        return []
    try:
        fields = pymupdf4llm.get_key_values(doc)
    except Exception:
        # Form data is auxiliary; a malformed AcroForm should not fail the request.
        log.warning("Could not read form fields", exc_info=True)
        return []
    return [
        {"name": name, "value": field.get("field_value"), "pages": [p + 1 for p in field.get("pages", [])]}
        for name, field in fields.items()
    ]


_PDF_DATE = re.compile(
    r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:([Zz+\-])(\d{2})?'?(\d{2})?'?)?"
)


def parse_pdf_date(value: str | None) -> str | None:
    """Convert a PDF date (``D:20240131235959+01'00'``) to ISO 8601.

    Returns the raw string if it is not a recognisable PDF date, so no
    information is lost, and ``None`` if it is empty.
    """
    if not value or not value.strip():
        return None
    match = _PDF_DATE.match(value.strip())
    if not match:
        return value
    year, month, day, hour, minute, second, sign, tz_hours, tz_minutes = match.groups()
    try:
        moment = datetime(
            int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0), int(second or 0)
        )
        if sign in ("Z", "z"):
            moment = moment.replace(tzinfo=timezone.utc)
        elif sign:
            offset = timedelta(hours=int(tz_hours or 0), minutes=int(tz_minutes or 0))
            moment = moment.replace(tzinfo=timezone(offset if sign == "+" else -offset))
    except ValueError:
        return value
    return moment.isoformat()


def _limit_onnx_threads(threads: int) -> None:
    """Cap ONNX Runtime's intra-op thread pool for the layout models.

    By default every session spins up one thread per core. Measured on 8
    cores, a 20-page document took the same wall time with 1 thread as with
    the default, but used 7x less CPU, so one thread per worker process lets
    the worker pool scale instead of oversubscribing the machine. The layout
    package exposes no setting for this; its sessions are built from
    ``onnxruntime.SessionOptions()`` at model-load time, so substituting a
    preconfigured subclass is the least invasive hook.
    """
    global _onnx_limited
    if _onnx_limited:
        return
    try:
        import onnxruntime
    except ImportError:
        return

    base = onnxruntime.SessionOptions

    class SessionOptions(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.intra_op_num_threads = threads
            self.inter_op_num_threads = 1
            self.add_session_config_entry("session.intra_op.allow_spinning", "0")

    onnxruntime.SessionOptions = SessionOptions
    _onnx_limited = True


def _tesseract_languages() -> tuple[str, ...] | None:
    try:
        tessdata = pymupdf.get_tessdata()
        files = os.listdir(tessdata)
    except Exception:
        return None  # OCR engine without Tesseract (e.g. RapidOCR only)
    return tuple(sorted(f.removesuffix(".traineddata") for f in files if f.endswith(".traineddata")))


def _warm_up() -> None:
    """Load the layout models now so the first real request is not slowed down."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "warm-up")
    try:
        pymupdf4llm.to_markdown(doc, use_ocr=OCRMode.NEVER)
    finally:
        doc.close()
        _release_memory()


def _release_memory() -> None:
    """Drop per-document state that would otherwise accumulate in a long-lived worker."""
    # pymupdf4llm collects notes in a module-level StringIO. Its own reset
    # truncates before seeking, so stale notes leak into the next document's
    # output (and are never cleared if parsing fails part-way).
    messages = getattr(document_layout, "INFO_MESSAGES", None)
    if isinstance(messages, io.StringIO):
        messages.seek(0)
        messages.truncate()
    pymupdf.TOOLS.reset_mupdf_warnings()
    pymupdf.TOOLS.store_shrink(100)
