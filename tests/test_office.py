"""Office format detection, decryption, properties, and LibreOffice hardening."""

import asyncio
import io
import os
import shutil
import sys
import time
import zipfile

import pymupdf
import pytest

from app import office
from app.errors import ServiceError
from app.pool import WorkerPool
from tests import worker_tasks
from tests.conftest import encrypt_ooxml, make_docx, make_epub, make_odt, make_pdf, make_pptx, make_xlsx


@pytest.mark.parametrize(
    ("make", "expected"),
    [(make_docx, office.DOCX), (make_xlsx, office.XLSX), (make_pptx, office.PPTX), (make_odt, office.ODT)],
)
def test_detects_modern_formats(make, expected):
    assert office.detect(make()) == office.Detected(expected, encrypted=False)


def test_detects_rtf():
    assert office.detect(b"{\\rtf1\\ansi Hello}").format is office.RTF


@pytest.mark.parametrize("data", [make_pdf(), make_epub(), b"PK\x03\x04 not really a zip", b"plain text", b""])
def test_leaves_other_files_to_mupdf(data):
    assert office.detect(data) is None


def test_ignores_zip_without_known_main_part():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("other/part.xml", "<x/>")
    assert office.detect(buffer.getvalue()) is None


def test_does_not_inflate_huge_members():
    # A "mimetype" member that inflates to 50 MB but is tiny on the wire.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", b"\0" * (50 * 1024 * 1024))
    assert len(buffer.getvalue()) < 200_000
    started = time.perf_counter()
    assert office.detect(buffer.getvalue()) is None
    assert time.perf_counter() - started < 0.5


def test_detects_encryption():
    assert office.detect(encrypt_ooxml(make_docx(), "pw")) == office.Detected(None, encrypted=True)
    assert office.detect(make_odt(encrypted=True)) == office.Detected(office.ODT, encrypted=True)


def test_decrypt():
    secret = encrypt_ooxml(make_docx(), "hunter2")
    with pytest.raises(ServiceError) as missing:
        office.decrypt(secret, None)
    with pytest.raises(ServiceError) as wrong:
        office.decrypt(secret, "nope")
    assert (missing.value.code, wrong.value.code) == ("password_required", "invalid_password")
    assert office.detect(office.decrypt(secret, "hunter2")).format is office.DOCX


def test_ooxml_properties_come_from_the_source_file():
    props = office.properties(make_docx(title="Memo", author="Grace"), office.DOCX)
    assert (props["title"], props["author"]) == ("Memo", "Grace")
    assert props["creation_date"] == "2023-05-17T09:30:00+00:00"


def test_legacy_formats(libreoffice):
    # Produce real DOC/XLS/PPT files with LibreOffice itself.
    for source, fmt, target in [(make_docx, office.DOCX, "doc"), (make_xlsx, office.XLSX, "xls"), (make_pptx, office.PPTX, "ppt")]:
        legacy = libreoffice.convert(source(), fmt, target)
        detected = office.detect(legacy)
        assert detected.format.extension == f".{target}" and not detected.encrypted


def test_linked_files_are_not_pulled_into_the_output(libreoffice, tmp_path):
    # With stock LibreOffice settings, an image *linked* from a document is
    # loaded from the server's disk and rendered into the PDF.
    canary = tmp_path / "canary.png"
    pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False).save(canary)
    linked = make_odt(
        "<text:p>Canary below.</text:p><text:p>"
        '<draw:frame draw:name="i" svg:width="2cm" svg:height="2cm">'
        f'<draw:image xlink:href="{canary.as_uri()}" xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/>'
        "</draw:frame></text:p>"
    )
    page = pymupdf.open(stream=libreoffice.convert(linked, office.ODT, "pdf"))[0]
    assert "Canary below." in page.get_text()
    assert page.get_images() == []


def test_unconvertible_document_is_a_client_error(libreoffice):
    with pytest.raises(ServiceError) as info:
        libreoffice.convert(b"PK\x03\x04 broken", office.DOCX, "pdf")
    assert info.value.code == "invalid_document"


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("setpriv"), reason="needs Linux setpriv")
def test_child_processes_die_with_a_killed_worker(tmp_path):
    """Pool timeout -> worker SIGKILLed -> LibreOffice must not live on as an orphan."""
    pid_file = tmp_path / "child.pid"

    async def scenario():
        pool = WorkerPool(1)
        await pool.start()
        try:
            with pytest.raises(ServiceError) as info:
                await pool.run(worker_tasks.spawn_child_then_hang, timeout=2, pid_file=str(pid_file))
            assert info.value.code == "processing_timeout"
        finally:
            await pool.close()

    asyncio.run(scenario())
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child), "child outlived its killed worker"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); check its state.
    with open(f"/proc/{pid}/stat") as f:
        return f.read().split(")")[-1].split()[0] != "Z"
