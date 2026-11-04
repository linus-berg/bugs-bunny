"""Document processing on top of pymupdf4llm.

Everything here runs inside worker processes (see ``pool.py``), never in the
web server process: PyMuPDF is not thread-safe, the layout analysis is
CPU-bound, and malformed documents can hang or crash MuPDF. Functions take
and return plain picklable values and signal client-facing failures by
raising :class:`ServiceError`.

PDFs, images and the other formats MuPDF reads go straight into the layout
pipeline. Office text documents and presentations are converted to PDF by
LibreOffice first; spreadsheets are read cell by cell (``spreadsheets.py``).
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import pymupdf
import pymupdf4llm
from pymupdf4llm.helpers import document_layout
from pymupdf4llm.ocr import OCRMode

from app import office, spreadsheets
from app.errors import ServiceError
from app.ocr import RAPIDOCR_LANGUAGES, Engine
from app.pages import PageRanges, resolve_pages

log = logging.getLogger(__name__)

OcrMode = Literal["auto", "off", "force"]
TableFormat = Literal["markdown", "html"]

# Set up once per worker by initialize(). Resolving OCR engines up front also
# avoids the library re-probing for them on every call, which spawns
# `tesseract`/`whereis` subprocesses when TESSDATA_PREFIX is unset.
_ocr_engines: dict[str, Callable] = {}
_libreoffice: office.LibreOffice | None = None
_onnx_limited = False


def initialize(onnx_threads: int = 1, office_timeout: float = 300.0) -> dict[str, Any]:
    """One-time worker setup; returns the capabilities this worker supports."""
    global _libreoffice
    # Library chatter ("OCR on page 3", ...) goes to logging, not raw stdout.
    pymupdf.set_messages(pylogging_name="pymupdf", pylogging_level=logging.DEBUG)
    if onnx_threads > 0:
        _limit_onnx_threads(onnx_threads)  # before any ONNX model, RapidOCR's included, is loaded
    engines = _load_ocr_engines()
    _warm_up()
    _libreoffice = _start_libreoffice(office_timeout)

    versions = {"pymupdf": pymupdf.__version__, "pymupdf4llm": pymupdf4llm.__version__}
    if "rapidocr" in _ocr_engines:
        versions["rapidocr"] = _package_version("rapidocr")
    if _libreoffice:
        versions["libreoffice"] = _libreoffice.version() or "unknown"
    office_formats = [f.name for f in office.ALL_FORMATS if _libreoffice or f is office.XLSX]
    return {
        "ocr_available": bool(engines),
        "ocr_engines": engines,
        "office_formats": office_formats,
        "versions": versions,
    }


def document_info(*, path: str, password: str | None = None) -> dict[str, Any]:
    """Metadata, outline and form fields. Cheap: no layout analysis."""
    with _open(path, password) as source:
        return source.info()


def convert(
    *,
    path: str,
    password: str | None = None,
    output: Literal["markdown", "text"] = "markdown",
    pages: PageRanges | None = None,
    max_pages: int = 0,
    ocr: OcrMode = "auto",
    ocr_engine: Engine | None = None,
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
    with _open(path, password) as source:
        info = source.info() if per_page else None
        selected = _select_pages(source.page_count, pages, max_pages)

        if source.sheets is not None:
            indices = selected if selected is not None else range(len(source.sheets))
            result = [
                {"page": i + 1, "content": spreadsheets.render(source.sheets[i], output, tables)} for i in indices
            ]
            if not per_page:
                return "\n".join(p["content"] for p in result)
            return {"document": info, "pages": result}

        options: dict[str, Any] = {
            "pages": selected,
            "header": page_headers,
            "footer": page_footers,
            "page_chunks": per_page,
            **_ocr_options(ocr, ocr_engine, ocr_language),
        }
        if output == "markdown":
            if tables == "html":
                options["table_output"] = "html"
            result = pymupdf4llm.to_markdown(source.doc, **options)
        else:
            result = pymupdf4llm.to_text(source.doc, **options)

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
    ocr_engine: Engine | None = None,
    ocr_language: str = "eng",
    tables: TableFormat = "markdown",
    max_tokens: int = 400,
    min_tokens: int = 120,
    table_mode: Literal["preserve", "isolate"] = "preserve",
    headers_footers: Literal["exclude", "auto", "include"] = "exclude",
    tagged: bool = False,
) -> dict[str, Any]:
    """Split the document into layout-aware chunks for retrieval (RAG)."""
    with _open(path, password) as source:
        info = source.info()
        selected = _select_pages(source.page_count, pages, max_pages)

        if source.sheets is not None:
            indices = selected if selected is not None else range(len(source.sheets))
            chunks = spreadsheets.chunk(
                [(i, source.sheets[i]) for i in indices],
                page_count=source.page_count,
                max_tokens=max_tokens,
                tables=tables,
                tagged=tagged,
            )
            return {"document": info, "chunks": chunks}

        chunked = pymupdf4llm.to_chunks(
            source.doc,
            pages=selected,
            force_text=True,  # match to_markdown's default
            table_output=tables,
            max_tokens=max_tokens,
            min_tokens=min_tokens,
            table_mode=table_mode,
            header_footer_mode=headers_footers,
            **_ocr_options(ocr, ocr_engine, ocr_language),
        )
        chunks = chunked.to_dicts(include_tagged=tagged)

    for c in chunks:
        c.get("metadata", {}).pop("file_path", None)  # always empty: documents are opened from memory
    return {"document": info, "chunks": chunks}


# --------------------------------------------------------------------------
# Opening documents
# --------------------------------------------------------------------------


@dataclass
class _Source:
    """An opened document: a PyMuPDF document, or the sheets of a spreadsheet."""

    doc: pymupdf.Document | None = None
    sheets: list[spreadsheets.Sheet] | None = None
    office_format: str | None = None
    encrypted: bool = False
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.sheets) if self.sheets is not None else self.doc.page_count

    def info(self) -> dict[str, Any]:
        if self.sheets is not None:
            info = {
                "format": self.office_format,
                "page_count": len(self.sheets),
                "encrypted": self.encrypted,
                **{k: None for k in ("title", "author", "subject", "keywords", "creator", "producer")},
                "creation_date": None,
                "modification_date": None,
                "toc": [{"level": 1, "title": s.name, "page": i + 1} for i, s in enumerate(self.sheets)],
                "form_fields": [],
            }
        else:
            info = _document_info(self.doc)
            if self.office_format is None:
                return info
            # The PDF came from LibreOffice: its producer and dates describe
            # the conversion, not the document.
            info.update(format=self.office_format, encrypted=self.encrypted, producer=None)
            info.update(creator=None, creation_date=None, modification_date=None)
        info.update({k: v for k, v in self.properties.items() if v is not None})
        return info


@contextlib.contextmanager
def _open(path: str, password: str | None) -> Iterator[_Source]:
    with open(path, "rb") as f:
        data = f.read()
    try:
        detected = office.detect(data)
        if detected is not None:
            source = _open_office(data, detected, password)
        else:
            source = _Source(doc=_open_pymupdf(data, password))
        try:
            if source.doc is not None and source.doc.page_count == 0:
                raise ServiceError(422, "invalid_document", "The document has no pages.")
            yield source
        finally:
            # pymupdf4llm closes the caller's document itself when it converts
            # an image to PDF for OCR, and closing twice raises.
            if source.doc is not None and not source.doc.is_closed:
                source.doc.close()
    finally:
        _release_memory()


def _open_pymupdf(data: bytes, password: str | None) -> pymupdf.Document:
    try:
        # Open from memory rather than from the path so that the server-side
        # temp file name never ends up in any output (pymupdf4llm echoes
        # doc.name). No filetype hint: MuPDF sniffs the content, so a
        # mislabelled file (a PNG uploaded as "scan.pdf") still works.
        doc = pymupdf.open(stream=data)
    except pymupdf.EmptyFileError:
        raise ServiceError(400, "empty_file", "The uploaded file is empty.") from None
    except Exception:
        raise ServiceError(
            422,
            "invalid_document",
            "The file could not be opened. It is not a valid PDF or other supported document.",
        ) from None
    if doc.needs_pass:
        if not password:
            doc.close()
            raise ServiceError(
                422,
                "password_required",
                "The document is encrypted. Provide its password in the 'password' form field.",
            )
        if not doc.authenticate(password):
            doc.close()
            raise ServiceError(422, "invalid_password", "The supplied password is incorrect.")
    return doc


def _open_office(data: bytes, detected: office.Detected, password: str | None) -> _Source:
    encrypted = detected.encrypted
    if encrypted:
        if detected.format in (office.ODT, office.ODP, office.ODS):
            raise ServiceError(
                422,
                "unsupported_encryption",
                "Password-protected OpenDocument files are not supported. Remove the password or export to PDF.",
            )
        data = office.decrypt(data, password)
        detected = office.detect(data)
        if detected is None or detected.encrypted or detected.format is None:
            raise ServiceError(422, "invalid_document", "The decrypted document is not a supported office file.")
    fmt = detected.format
    properties = office.properties(data, fmt)

    if fmt.kind == "spreadsheet":
        if fmt is not office.XLSX:
            data = _require_libreoffice(fmt).convert(data, fmt, "xlsx")
        try:
            sheets = spreadsheets.load(data)
        except ServiceError as error:
            # e.g. "Strict Open XML", which openpyxl cannot read: normalise it.
            if fmt is not office.XLSX or error.code != "invalid_document" or _libreoffice is None:
                raise
            sheets = spreadsheets.load(_libreoffice.convert(data, fmt, "xlsx"))
        return _Source(sheets=sheets, office_format=fmt.name, encrypted=encrypted, properties=properties)

    pdf = _require_libreoffice(fmt).convert(data, fmt, "pdf")
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    return _Source(doc=doc, office_format=fmt.name, encrypted=encrypted, properties=properties)


def _require_libreoffice(fmt: office.OfficeFormat) -> office.LibreOffice:
    if _libreoffice is None:
        raise ServiceError(
            422,
            "unsupported_format",
            f"{fmt.name} files need LibreOffice, which is not installed on this server.",
        )
    return _libreoffice


def _select_pages(page_count: int, ranges: PageRanges | None, max_pages: int) -> list[int] | None:
    if ranges:
        try:
            selected: list[int] | None = resolve_pages(ranges, page_count)
        except ValueError as e:
            raise ServiceError(422, "invalid_page_range", str(e)) from None
        count = len(selected)
    else:
        selected, count = None, page_count

    if max_pages and count > max_pages:
        raise ServiceError(
            422,
            "too_many_pages",
            f"{count} pages selected but this server processes at most {max_pages} per request. "
            "Use the 'pages' parameter to process the document in parts.",
        )
    return selected


def _ocr_options(mode: OcrMode, engine: Engine | None, language: str) -> dict[str, Any]:
    """Library options for OCR. The web process has already picked an engine that suits ``language``."""
    if mode == "off" or engine is None:
        return {"use_ocr": OCRMode.NEVER}
    function = _ocr_engines.get(engine)
    if function is None:
        raise ServiceError(422, "ocr_engine_unavailable", f"The {engine} OCR engine is not installed on this server.")
    options: dict[str, Any] = {
        # Only OCR pages that need it, and keep text layers that previous OCR
        # already produced instead of redoing the work.
        "use_ocr": OCRMode.SELECT_KEEP_OLD,
        "ocr_language": language,
        "ocr_function": function,
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


def _load_ocr_engines() -> dict[str, dict[str, Any]]:
    """Find the installed OCR engines and load their models now, not on the first scan."""
    _ocr_engines.clear()
    engines: dict[str, dict[str, Any]] = {}
    try:
        with contextlib.redirect_stdout(io.StringIO()):  # it announces its backend on import
            from pymupdf4llm.ocr import rapidocr_api
        if rapidocr_api.full_ocr is not None:
            import numpy

            doc = pymupdf.open()
            doc.new_page(width=200, height=40).insert_text((10, 28), "warm-up", fontsize=20)
            pixmap = doc[0].get_pixmap()
            image = numpy.frombuffer(pixmap.samples, dtype=numpy.uint8).reshape(pixmap.h, pixmap.w, pixmap.n)
            rapidocr_api.full_ocr(image)
            doc.close()
            _ocr_engines["rapidocr"] = rapidocr_api.exec_ocr
            engines["rapidocr"] = {"languages": sorted(RAPIDOCR_LANGUAGES)}
    except Exception:
        log.warning("RapidOCR is installed but not usable", exc_info=True)

    languages = _tesseract_languages()
    if languages:
        try:
            from pymupdf4llm.ocr import tesseract_api

            _ocr_engines["tesseract"] = tesseract_api.exec_ocr
            engines["tesseract"] = {"languages": list(languages)}
        except Exception:
            log.warning("Tesseract language data found but Tesseract OCR is not usable", exc_info=True)
    return engines


def _tesseract_languages() -> tuple[str, ...]:
    try:
        files = os.listdir(pymupdf.get_tessdata())
    except Exception:
        return ()
    # "osd" detects orientation and script; it is not a language to read.
    names = (f.removesuffix(".traineddata") for f in files if f.endswith(".traineddata"))
    return tuple(sorted(n for n in names if n != "osd"))


def _start_libreoffice(timeout: float) -> office.LibreOffice | None:
    binary = office.find_libreoffice()
    if binary is None:
        return None
    libreoffice = office.LibreOffice(binary, timeout)
    try:
        # Initialises this worker's profile and checks LibreOffice actually works.
        libreoffice.convert(b"{\\rtf1 warm-up}", office.RTF, "pdf")
    except Exception:
        log.exception("LibreOffice is installed but not working; office documents other than XLSX are disabled")
        return None
    return libreoffice


def _package_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


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
