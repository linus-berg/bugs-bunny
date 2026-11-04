"""Office documents: detection, decryption, properties and LibreOffice conversion.

Runs inside worker processes. Text documents and presentations are converted
to PDF and then go through the normal layout pipeline. Spreadsheets are
handled by ``spreadsheets.py`` instead: rendered to PDF, cells are clipped to
their column width, which silently loses data.
"""

from __future__ import annotations

import atexit
import contextlib
import io
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

import msoffcrypto
import olefile
from msoffcrypto.exceptions import InvalidKeyError

from app.errors import ServiceError

log = logging.getLogger(__name__)

Kind = Literal["text", "presentation", "spreadsheet"]


@dataclass(frozen=True)
class OfficeFormat:
    name: str
    kind: Kind
    extension: str  # LibreOffice picks its import filter partly by extension


DOCX = OfficeFormat("DOCX", "text", ".docx")
DOC = OfficeFormat("DOC", "text", ".doc")
ODT = OfficeFormat("ODT", "text", ".odt")
RTF = OfficeFormat("RTF", "text", ".rtf")
PPTX = OfficeFormat("PPTX", "presentation", ".pptx")
PPT = OfficeFormat("PPT", "presentation", ".ppt")
ODP = OfficeFormat("ODP", "presentation", ".odp")
XLSX = OfficeFormat("XLSX", "spreadsheet", ".xlsx")
XLSB = OfficeFormat("XLSB", "spreadsheet", ".xlsb")
XLS = OfficeFormat("XLS", "spreadsheet", ".xls")
ODS = OfficeFormat("ODS", "spreadsheet", ".ods")

ALL_FORMATS = (DOCX, DOC, ODT, RTF, PPTX, PPT, ODP, XLSX, XLSB, XLS, ODS)

_OOXML_MAIN_PARTS = {
    "word/document.xml": DOCX,
    "ppt/presentation.xml": PPTX,
    "xl/workbook.xml": XLSX,
    "xl/workbook.bin": XLSB,
}
_ODF_MIMETYPES = {
    "application/vnd.oasis.opendocument.text": ODT,
    "application/vnd.oasis.opendocument.presentation": ODP,
    "application/vnd.oasis.opendocument.spreadsheet": ODS,
}
_OLE_STREAMS = {"WordDocument": DOC, "PowerPoint Document": PPT, "Workbook": XLS, "Book": XLS}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAX_METADATA_PART = 1024 * 1024  # never inflate more than this from an untrusted archive member


@dataclass(frozen=True)
class Detected:
    format: OfficeFormat | None  # None for encrypted OOXML: the real type is only known after decryption
    encrypted: bool


def detect(data: bytes) -> Detected | None:
    """Recognise an office document by content. ``None`` means "not an office file"."""
    if data.startswith(b"PK\x03\x04"):
        return _detect_zip(data)
    if data.startswith(_OLE_MAGIC):
        return _detect_ole(data)
    if data.startswith(b"{\\rtf"):
        return Detected(RTF, encrypted=False)
    return None


def decrypt(data: bytes, password: str | None) -> bytes:
    """Decrypt a password-protected Microsoft Office file."""
    if not password:
        raise ServiceError(
            422, "password_required", "The document is encrypted. Provide its password in the 'password' form field."
        )
    try:
        office_file = msoffcrypto.OfficeFile(io.BytesIO(data))
        office_file.load_key(password=password, verify_password=True)
        out = io.BytesIO()
        office_file.decrypt(out)
    except InvalidKeyError:
        raise ServiceError(422, "invalid_password", "The supplied password is incorrect.") from None
    except Exception:
        log.warning("Could not decrypt office document", exc_info=True)
        raise ServiceError(422, "unsupported_encryption", "This document's encryption method is not supported.") from None
    return out.getvalue()


def properties(data: bytes, fmt: OfficeFormat) -> dict[str, Any]:
    """Document properties as stored in the source file.

    The PDF that LibreOffice produces would report "LibreOffice" as producer
    and the moment of conversion as creation date, so read the original.
    """
    try:
        if fmt in (DOCX, PPTX, XLSX, XLSB):
            return _ooxml_properties(data)
        if fmt in (ODT, ODP, ODS):
            return _odf_properties(data)
        if fmt in (DOC, PPT, XLS):
            return _ole_properties(data)
    except Exception:
        log.warning("Could not read %s properties", fmt.name, exc_info=True)
    return {}


# --------------------------------------------------------------------------
# LibreOffice
# --------------------------------------------------------------------------

# Hardened profile settings. Verified with canary documents: with stock
# settings, an image *linked* (not embedded) from a document is fetched and
# rendered into the output, i.e. an uploaded file can read other files on
# the server. Blocking untrusted links stops that. Macros, OLE and DDE are
# disabled, and links are never updated on load.
_REGISTRY = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>1</value></prop></item>
</oor:items>
"""

_RESTART_REQUESTED = 81  # soffice.bin's exit code after initialising a fresh profile
_PROFILES_ROOT = Path(tempfile.gettempdir()) / "pdf-service-libreoffice"


def find_libreoffice() -> str | None:
    """Path of ``soffice.bin``.

    The ``soffice`` launcher forks the real binary as a grandchild, so
    killing the launcher would leave LibreOffice running. Call the binary
    directly.
    """
    candidates = []
    if launcher := shutil.which("soffice"):
        candidates.append(Path(os.path.realpath(launcher)).with_name("soffice.bin"))
    candidates += [
        Path("/usr/lib/libreoffice/program/soffice.bin"),
        Path("/usr/lib64/libreoffice/program/soffice.bin"),
        Path("/opt/libreoffice/program/soffice.bin"),
    ]
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


class LibreOffice:
    """Runs one conversion at a time; each worker process owns one instance."""

    def __init__(self, binary: str, timeout: float) -> None:
        self.binary = binary
        self.timeout = timeout
        self._profile: Path | None = None
        # Make LibreOffice die with this worker. The pool SIGKILLs workers
        # that exceed their timeout, and a child would otherwise keep running.
        self._prefix = ["setpriv", "--pdeathsig", "KILL"] if sys.platform == "linux" and shutil.which("setpriv") else []

    def version(self) -> str | None:
        try:
            result = subprocess.run(
                [*self._prefix, self.binary, "--version"], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        words = result.stdout.split()
        return words[1] if len(words) > 1 and words[0] == "LibreOffice" else None

    def convert(self, data: bytes, fmt: OfficeFormat, target: Literal["pdf", "xlsx"]) -> bytes:
        with tempfile.TemporaryDirectory(prefix="office-") as tmp:
            source = Path(tmp) / f"document{fmt.extension}"
            source.write_bytes(data)
            output = source.with_suffix(f".{target}")
            for _ in range(2):
                result = self._run(["--convert-to", target, "--outdir", tmp, str(source)])
                if result.returncode != _RESTART_REQUESTED:
                    break
            if result.returncode != 0 or not output.is_file():
                log.warning(
                    "LibreOffice could not convert a %s file (exit code %s): %s",
                    fmt.name,
                    result.returncode,
                    result.stderr.decode(errors="replace").strip()[-500:],
                )
                raise ServiceError(
                    422,
                    "invalid_document",
                    f"The {fmt.name} file could not be converted. It may be damaged or use unsupported features.",
                )
            return output.read_bytes()

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        profile = self._ensure_profile()
        command = [
            *self._prefix,
            self.binary,
            "--headless",
            "--invisible",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation={profile.as_uri()}",
            *args,
        ]
        try:
            return subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.timeout,
                env={**os.environ, "HOME": str(profile)},
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ServiceError(
                504, "processing_timeout", f"Converting the document took longer than {self.timeout:g} seconds."
            ) from None

    def _ensure_profile(self) -> Path:
        """A private LibreOffice profile per worker: concurrent instances can't share one."""
        if self._profile is None:
            if _own_profiles_root():
                _remove_stale_profiles()
                profile = _PROFILES_ROOT / str(os.getpid())
                shutil.rmtree(profile, ignore_errors=True)
                profile.mkdir(mode=0o700)
            else:
                profile = Path(tempfile.mkdtemp(prefix="libreoffice-profile-"))
            (profile / "user").mkdir(mode=0o700)
            (profile / "user" / "registrymodifications.xcu").write_text(_REGISTRY)
            atexit.register(shutil.rmtree, profile, ignore_errors=True)
            self._profile = profile
        return self._profile


def _own_profiles_root() -> bool:
    """Create the shared profiles directory, refusing one another user planted in /tmp."""
    try:
        _PROFILES_ROOT.mkdir(mode=0o700, exist_ok=True)
        info = _PROFILES_ROOT.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


def _remove_stale_profiles() -> None:
    """Delete profiles left behind by workers that were killed."""
    with contextlib.suppress(OSError):
        for path in _PROFILES_ROOT.iterdir():
            if path.name.isdigit() and not _pid_alive(int(path.name)):
                shutil.rmtree(path, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------
# Detection and properties
# --------------------------------------------------------------------------


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _MAX_METADATA_PART:  # zip bomb guard
        return None
    return archive.read(info)


def _detect_zip(data: bytes) -> Detected | None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = set(archive.namelist())
        mimetype = _read_member(archive, "mimetype")
        if mimetype is not None:
            fmt = _ODF_MIMETYPES.get(mimetype.decode("ascii", "replace").strip())
            if fmt is None:
                return None  # EPUB and other zip-based formats MuPDF reads natively
            manifest = _read_member(archive, "META-INF/manifest.xml") or b""
            return Detected(fmt, encrypted=b"encryption-data" in manifest)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        return None
    if "[Content_Types].xml" not in names:
        return None
    return next((Detected(fmt, False) for part, fmt in _OOXML_MAIN_PARTS.items() if part in names), None)


def _detect_ole(data: bytes) -> Detected | None:
    try:
        with olefile.OleFileIO(data) as ole:
            streams = {"/".join(entry) for entry in ole.listdir()}
    except Exception:
        return None
    if "EncryptedPackage" in streams:  # a password-protected DOCX, XLSX or PPTX
        return Detected(None, encrypted=True)
    fmt = next((fmt for stream, fmt in _OLE_STREAMS.items() if stream in streams), None)
    if fmt is None:
        return None  # e.g. an Outlook message
    try:
        encrypted = msoffcrypto.OfficeFile(io.BytesIO(data)).is_encrypted()
    except Exception:
        encrypted = False
    return Detected(fmt, encrypted)


_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
}


def _text(root: ElementTree.Element | None, path: str) -> str | None:
    if root is None:
        return None
    node = root.find(path, _NS)
    return (node.text or "").strip() or None if node is not None else None


def _xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element | None:
    raw = _read_member(archive, name)
    return ElementTree.fromstring(raw) if raw else None


def _ooxml_properties(data: bytes) -> dict[str, Any]:
    archive = zipfile.ZipFile(io.BytesIO(data))
    core, app = _xml(archive, "docProps/core.xml"), _xml(archive, "docProps/app.xml")
    return {
        "title": _text(core, "dc:title"),
        "author": _text(core, "dc:creator"),
        "subject": _text(core, "dc:subject"),
        "keywords": _text(core, "cp:keywords"),
        "creator": _text(app, "ep:Application"),
        "creation_date": _iso(_text(core, "dcterms:created")),
        "modification_date": _iso(_text(core, "dcterms:modified")),
    }


def _odf_properties(data: bytes) -> dict[str, Any]:
    meta = _xml(zipfile.ZipFile(io.BytesIO(data)), "meta.xml")
    keywords = [k.text.strip() for k in meta.iterfind(".//meta:keyword", _NS) if k.text] if meta is not None else []
    return {
        "title": _text(meta, ".//dc:title"),
        "author": _text(meta, ".//meta:initial-creator") or _text(meta, ".//dc:creator"),
        "subject": _text(meta, ".//dc:subject"),
        "keywords": ", ".join(keywords) or None,
        "creator": _text(meta, ".//meta:generator"),
        "creation_date": _iso(_text(meta, ".//meta:creation-date")),
        "modification_date": _iso(_text(meta, ".//dc:date")),
    }


def _ole_properties(data: bytes) -> dict[str, Any]:
    with olefile.OleFileIO(data) as ole:
        meta = ole.get_metadata()
    encoding = "utf-8" if meta.codepage == 65001 else f"cp{meta.codepage or 1252}"
    try:
        "".encode(encoding)
    except LookupError:
        encoding = "cp1252"

    def text(value: bytes | None) -> str | None:
        return value.decode(encoding, "replace").strip() or None if value else None

    def date(value: datetime | None) -> str | None:
        return value.replace(tzinfo=timezone.utc).isoformat() if isinstance(value, datetime) else None

    return {
        "title": text(meta.title),
        "author": text(meta.author),
        "subject": text(meta.subject),
        "keywords": text(meta.keywords),
        "creator": text(meta.creating_application),
        "creation_date": date(meta.create_time),
        "modification_date": date(meta.last_saved_time),
    }


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value
