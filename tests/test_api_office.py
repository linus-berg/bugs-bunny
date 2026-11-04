"""End-to-end: office documents and OCR engine selection through the API."""

import io

import pytest

from tests.conftest import encrypt_ooxml, make_docx, make_odt, make_pptx, make_scanned_png, make_xlsx
from tests.test_api import assert_error, upload


# -- spreadsheets (read directly; no LibreOffice needed for XLSX) ------------


def test_xlsx_converts_to_tables_without_losing_cell_text(client):
    response = client.post("/convert", **upload(make_xlsx(), name="sales.xlsx"))
    assert response.status_code == 200
    text = response.text
    assert text.startswith("## Orders\n\n|Order|Customer|Region|Total|")
    # Rendered to PDF, Calc clips this to the column width ("Customer").
    assert "|SO-1013|Customer number 13|South|136.5|" in text
    assert "## Summary" in text


def test_xlsx_extract_treats_sheets_as_pages(client):
    body = client.post("/extract", params={"pages": "2"}, **upload(make_xlsx())).json()
    assert [p["page"] for p in body["pages"]] == [2]
    assert body["pages"][0]["content"].startswith("## Summary")
    document = body["document"]
    assert (document["format"], document["page_count"], document["title"]) == ("XLSX", 2, "Sales 2026")
    assert [entry["title"] for entry in document["toc"]] == ["Orders", "Summary"]


def test_xlsx_chunks_are_row_groups_with_headers(client):
    body = client.post("/chunks", params={"max_tokens": 100, "min_tokens": 0}, **upload(make_xlsx())).json()
    orders = [c for c in body["chunks"] if c["metadata"]["sheet"] == "Orders"]
    assert len(orders) > 1
    assert all(c["text"].startswith("## Orders\n\n|Order|Customer|") for c in orders)
    assert orders[0]["metadata"]["row_start"] == 2


# -- documents and presentations via LibreOffice ------------------------------


def test_docx(client, office_available):
    response = client.post("/extract", **upload(make_docx(), name="memo.docx"))
    assert response.status_code == 200, response.text
    body = response.json()
    content = body["pages"][0]["content"]
    assert "Quarterly Memo" in content and "Revenue grew by twelve percent" in content
    assert "Apple" in content and "Pear" in content
    document = body["document"]
    assert (document["format"], document["title"], document["author"]) == ("DOCX", "Quarterly Memo", "Ada Lovelace")
    # From the DOCX itself, not the conversion: no "LibreOffice" producer, no "today" date.
    assert document["creation_date"] == "2023-05-17T09:30:00+00:00"
    assert document["producer"] is None


def test_docx_in_other_scripts(client, office_available):
    import docx

    lines = ["今年的收入增长了百分之十二", "売上は十二パーセント増加しました", "매출이 12퍼센트 증가했습니다", "Выручка выросла", "Τα έσοδα αυξήθηκαν"]
    document = docx.Document()
    for line in lines:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    text = client.post("/convert", params={"format": "text"}, **upload(buffer.getvalue())).text
    # Without a font that has the glyphs, every CJK character comes out as the same wrong one.
    # Compare ignoring whitespace: MuPDF can drop the space between Hangul and digits set in
    # different fonts.
    squashed = "".join(text.split())
    assert [line for line in lines if "".join(line.split()) not in squashed] == []


def test_pptx_slides_become_pages(client, office_available):
    body = client.post("/extract", **upload(make_pptx(), name="deck.pptx")).json()
    assert body["document"]["format"] == "PPTX"
    assert [p["page"] for p in body["pages"]] == [1, 2]
    assert "Roadmap 2027" in body["pages"][0]["content"]
    assert "Risks" in body["pages"][1]["content"]


def test_odt_and_chunks(client, office_available):
    # Several paragraphs: the layout model files a lone one-line page under "page-footer",
    # which /chunks drops by default.
    paragraphs = "".join(
        f"<text:p>OpenDocument paragraph {i}, with enough words to read as body text.</text:p>" for i in range(1, 6)
    )
    response = client.post("/chunks", **upload(make_odt(paragraphs), name="a.odt"))
    assert response.status_code == 200
    body = response.json()
    assert body["document"]["format"] == "ODT"
    assert "OpenDocument paragraph 3" in " ".join(c["text"] for c in body["chunks"])


def test_encrypted_docx(client, office_available):
    secret = encrypt_ooxml(make_docx(), "hunter2")
    assert_error(client.post("/convert", **upload(secret)), 422, "password_required")
    assert_error(client.post("/convert", **upload(secret, password="nope")), 422, "invalid_password")
    response = client.post("/convert", **upload(secret, password="hunter2"))
    assert response.status_code == 200 and "Quarterly Memo" in response.text
    assert client.post("/metadata", **upload(secret, password="hunter2")).json()["encrypted"] is True


def test_encrypted_xlsx_needs_no_libreoffice(client):
    secret = encrypt_ooxml(make_xlsx(), "hunter2")
    response = client.post("/convert", **upload(secret, password="hunter2"))
    assert response.status_code == 200 and "Customer number 13" in response.text


def test_encrypted_opendocument_is_unsupported(client):
    assert_error(client.post("/convert", **upload(make_odt(encrypted=True))), 422, "unsupported_encryption")


def test_office_page_limit_applies_after_conversion(client, office_available, settings):
    import docx

    document = docx.Document()
    for i in range(settings.max_pages + 2):
        document.add_paragraph(f"Page {i + 1}")
        document.add_page_break()
    buffer = io.BytesIO()
    document.save(buffer)
    assert_error(client.post("/convert", **upload(buffer.getvalue())), 422, "too_many_pages")
    assert client.post("/convert", params={"pages": "1-2"}, **upload(buffer.getvalue())).status_code == 200


# -- OCR engines ---------------------------------------------------------------


def test_info_lists_engines_and_office_formats(client):
    body = client.get("/info").json()
    assert "XLSX" in body["office_formats"]
    ocr = body["ocr"]
    assert ocr["default_engine"] == "auto"
    for engine, details in ocr["engines"].items():
        assert engine in ("rapidocr", "tesseract")
        assert set(details["languages"]) <= set(ocr["languages"])


def _engines(client) -> dict:
    return client.get("/info").json()["ocr"]["engines"]


@pytest.mark.parametrize("engine", ["rapidocr", "tesseract"])
def test_explicit_engine_reads_a_scan(client, engine):
    if engine not in _engines(client):
        pytest.skip(f"{engine} is not installed")
    response = client.post("/convert", params={"ocr_engine": engine}, **upload(make_scanned_png(), name="s.png"))
    assert response.status_code == 200
    assert "INVOICE" in response.text.upper() and "4711" in response.text


@pytest.mark.parametrize("engine", ["rapidocr", "tesseract"])
def test_missing_engine_is_reported(client, pdf, engine):
    engines = _engines(client)
    if engine in engines or not engines:
        pytest.skip(f"{engine} is installed" if engine in engines else "no OCR engine at all")
    response = client.post("/convert", params={"ocr_engine": engine}, **upload(pdf))
    assert_error(response, 422, "ocr_engine_unavailable")


def test_language_no_engine_supports_is_rejected(client, pdf):
    if not _engines(client):
        pytest.skip("no OCR engine installed")
    response = client.post("/convert", params={"ocr_language": "ara"}, **upload(pdf))
    assert "ara" in assert_error(response, 422, "unsupported_ocr_language")["detail"]


def test_ocr_off_ignores_engine_and_language(client, pdf):
    response = client.post("/convert", params={"ocr": "off", "ocr_language": "ara"}, **upload(pdf))
    assert response.status_code == 200


def test_office_formats_without_libreoffice_are_rejected_clearly(client):
    if "DOCX" in client.get("/info").json()["office_formats"]:
        pytest.skip("LibreOffice is installed")
    body = assert_error(client.post("/convert", **upload(make_docx())), 422, "unsupported_format")
    assert "LibreOffice" in body["detail"]
