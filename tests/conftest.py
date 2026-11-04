from __future__ import annotations

import datetime
import io
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import office
from app.config import Settings
from app.main import create_app

API_KEY = "test-key"


def make_pdf(
    pages: int = 3,
    *,
    password: str | None = None,
    metadata: dict | None = None,
    toc: list | None = None,
) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 80), f"Chapter {i + 1}", fontsize=24)
        page.insert_text((72, 120), f"Body text of page {i + 1}, long enough to form a paragraph.", fontsize=11)
    doc.set_metadata(metadata or {"title": "Test Document", "author": "Ada Lovelace"})
    if toc:
        doc.set_toc(toc)
    options = {}
    if password:
        options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": password, "owner_pw": password + "-owner"}
    return doc.tobytes(**options)


def make_form_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Application form", fontsize=20)
    for name, kind, rect, value in [
        ("full_name", pymupdf.PDF_WIDGET_TYPE_TEXT, (72, 100, 300, 120), "Ada Lovelace"),
        ("agree", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, (72, 130, 90, 148), True),
    ]:
        widget = pymupdf.Widget()
        widget.field_name, widget.field_type = name, kind
        widget.rect, widget.field_value = pymupdf.Rect(rect), value
        page.add_widget(widget)
    return doc.tobytes()


def make_table_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    rows = [("Name", "Qty", "Price"), ("Apple", "3", "1.20"), ("Pear", "5", "0.80"), ("Plum", "7", "2.10")]
    x0, y0, widths, height = 72, 100, (150, 80, 80), 24
    for r, row in enumerate(rows):
        x = x0
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(x, y0 + r * height, x + widths[c], y0 + (r + 1) * height)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 4, rect.y1 - 7), cell, fontsize=11)
            x += widths[c]
    return doc.tobytes()


def make_epub() -> bytes:
    container = (
        '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    package = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">x</dc:identifier>'
        "<dc:title>Tiny Book</dc:title><dc:language>en</dc:language></metadata>"
        '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    chapter = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>c1</title></head>'
        "<body><h1>Once upon a time</h1><p>There was an EPUB paragraph.</p></body></html>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as epub:
        epub.writestr("mimetype", "application/epub+zip")  # must come first
        epub.writestr("META-INF/container.xml", container)
        epub.writestr("book.opf", package)
        epub.writestr("c1.xhtml", chapter)
    return buffer.getvalue()


def make_scanned_png(text: str = "SCANNED INVOICE 4711") -> bytes:
    """A picture of text with no text layer, as a scanner would produce."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=200)
    page.insert_text((40, 110), text, fontsize=36)
    return page.get_pixmap(dpi=200).tobytes("png")


def make_docx(*, title: str = "Quarterly Memo", author: str = "Ada Lovelace") -> bytes:
    import docx

    document = docx.Document()
    properties = document.core_properties
    properties.title, properties.author = title, author
    properties.created = datetime.datetime(2023, 5, 17, 9, 30)
    document.add_heading("Quarterly Memo", 1)
    document.add_paragraph("Revenue grew by twelve percent this quarter.")
    table = document.add_table(rows=3, cols=2)
    table.style = "Table Grid"
    for r, row in enumerate([("Item", "Qty"), ("Apple", "3"), ("Pear", "5")]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_xlsx() -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    orders = workbook.active
    orders.title = "Orders"
    orders.append(["Order", "Customer", "Region", "Total"])
    for i in range(1, 41):
        orders.append([f"SO-{1000 + i}", f"Customer number {i}", ["North", "South"][i % 2], i * 10.5])
    summary = workbook.create_sheet("Summary")
    summary.append(["Metric", "Value"])
    summary.append(["Orders", 40])
    workbook.properties.title, workbook.properties.creator = "Sales 2026", "Ada Lovelace"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_pptx() -> bytes:
    import pptx

    presentation = pptx.Presentation()
    for title, body in [("Roadmap 2027", "Launch the extraction service"), ("Risks", "Scanned documents")]:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def make_odt(body_xml: str = "<text:p>Hello from OpenDocument.</text:p>", *, encrypted: bool = False) -> bytes:
    """A minimal ODT; ``body_xml`` goes inside <office:text>."""
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" office:version="1.3">'
        f"<office:body><office:text>{body_xml}</office:text></office:body></office:document-content>"
    )
    encryption = "<manifest:encryption-data/>" if encrypted else ""
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
        '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
        f'<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">{encryption}'
        "</manifest:file-entry></manifest:manifest>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as odt:
        odt.writestr("mimetype", "application/vnd.oasis.opendocument.text")  # must come first
        odt.writestr("META-INF/manifest.xml", manifest)
        odt.writestr("content.xml", content)
    return buffer.getvalue()


def encrypt_ooxml(data: bytes, password: str) -> bytes:
    from msoffcrypto.format.ooxml import OOXMLFile

    out = io.BytesIO()
    OOXMLFile(io.BytesIO(data)).encrypt(password, out)
    return out.getvalue()


@pytest.fixture(scope="session")
def libreoffice() -> office.LibreOffice:
    binary = office.find_libreoffice()
    if binary is None:
        pytest.skip("LibreOffice is not installed")
    return office.LibreOffice(binary, timeout=120)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        workers=2,
        max_upload_mb=1,
        max_pages=5,
        processing_timeout=60,
        api_keys=(API_KEY,),
    )


@pytest.fixture(scope="session")
def client(settings):
    """One app (with real worker processes) shared by all API tests."""
    app = create_app(settings)
    with TestClient(app, headers={"X-API-Key": API_KEY}) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def ocr_available(client) -> bool:
    return client.get("/info").json()["ocr"]["available"]


@pytest.fixture(scope="session")
def office_available(client) -> None:
    if "DOCX" not in client.get("/info").json()["office_formats"]:
        pytest.skip("LibreOffice is not installed")


@pytest.fixture
def pdf() -> bytes:
    return make_pdf()
