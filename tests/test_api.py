"""End-to-end API tests against real worker processes."""

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.main import APIKeyMiddleware
from tests.conftest import (
    API_KEY,
    make_epub,
    make_form_pdf,
    make_pdf,
    make_scanned_png,
    make_table_pdf,
)


def upload(content: bytes, name: str = "doc.pdf", password: str | None = None) -> dict:
    request = {"files": {"file": (name, content, "application/pdf")}}
    if password is not None:
        request["data"] = {"password": password}
    return request


def assert_error(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    return body


# -- /convert ---------------------------------------------------------------


def test_convert_returns_markdown_as_plain_text_by_default(client, pdf):
    # The original API: POST a file, get Markdown back as text/plain.
    response = client.post("/convert", **upload(pdf))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "# Chapter 1" in response.text
    assert "Body text of page 3" in response.text


def test_convert_plain_text(client, pdf):
    response = client.post("/convert", params={"format": "text"}, **upload(pdf))
    assert response.status_code == 200
    assert "Chapter 1" in response.text
    assert "#" not in response.text


def test_convert_selected_pages(client, pdf):
    response = client.post("/convert", params={"pages": "2-"}, **upload(pdf))
    assert response.status_code == 200
    assert "Chapter 1" not in response.text
    assert "Chapter 2" in response.text and "Chapter 3" in response.text


def test_convert_html_tables(client):
    markdown = client.post("/convert", **upload(make_table_pdf())).text
    html = client.post("/convert", params={"tables": "html"}, **upload(make_table_pdf())).text
    assert "|" in markdown and "Apple" in markdown
    assert "<table" in html and "Apple" in html


def test_convert_without_page_headers_and_footers(client, pdf):
    response = client.post("/convert", params={"page_headers": "false", "page_footers": "false"}, **upload(pdf))
    assert response.status_code == 200
    assert "Body text of page 2" in response.text


def test_other_document_formats(client):
    response = client.post("/extract", **upload(make_epub(), name="book.epub"))
    assert response.status_code == 200
    body = response.json()
    assert body["document"]["format"] == "EPUB"
    assert "Once upon a time" in body["pages"][0]["content"]


def test_content_is_sniffed_regardless_of_filename(client):
    # A PNG named .pdf is still opened (as an image); a non-document is rejected cleanly.
    image = client.post("/metadata", **upload(make_scanned_png(), name="scan.pdf"))
    assert image.status_code == 200
    assert image.json()["format"] == "Image"
    assert_error(client.post("/convert", **upload(b"just some text", name="notes.pdf")), 422, "invalid_document")


# -- /extract ---------------------------------------------------------------


def test_extract_returns_pages_and_metadata(client):
    pdf = make_pdf(
        metadata={"title": "Annual Report", "author": "Ada", "creationDate": "D:20240131120000Z"},
        toc=[[1, "Intro", 1], [2, "Details", 2]],
    )
    response = client.post("/extract", params={"pages": "1,3"}, **upload(pdf, name="report.pdf"))
    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "markdown"
    assert [p["page"] for p in body["pages"]] == [1, 3]
    assert "Chapter 3" in body["pages"][1]["content"]
    document = body["document"]
    assert document["filename"] == "report.pdf"
    assert document["page_count"] == 3
    assert document["title"] == "Annual Report"
    assert document["creation_date"] == "2024-01-31T12:00:00+00:00"
    assert document["toc"] == [{"level": 1, "title": "Intro", "page": 1}, {"level": 2, "title": "Details", "page": 2}]


# -- /chunks ----------------------------------------------------------------


def test_chunks(client, pdf):
    response = client.post("/chunks", params={"max_tokens": 50, "min_tokens": 0, "tagged": True}, **upload(pdf))
    assert response.status_code == 200
    body = response.json()
    assert body["document"]["page_count"] == 3
    chunks = body["chunks"]
    assert len(chunks) >= 3
    first = chunks[0]
    assert "Chapter 1" in first["text"]
    assert first["metadata"]["page_start"] == 1
    assert first["metadata"]["section_path"] == ["Chapter 1"]
    assert "file_path" not in first["metadata"]
    assert first["tagged_content"].startswith("[Section] Chapter 1")


def test_chunks_validate_token_bounds(client, pdf):
    response = client.post("/chunks", params={"max_tokens": 10, "min_tokens": 20}, **upload(pdf))
    assert_error(response, 422, "invalid_request")


# -- /metadata --------------------------------------------------------------


def test_metadata(client, pdf):
    response = client.post("/metadata", **upload(pdf, name="a.pdf"))
    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "a.pdf"
    assert body["format"].startswith("PDF")
    assert (body["page_count"], body["encrypted"]) == (3, False)
    assert (body["title"], body["author"]) == ("Test Document", "Ada Lovelace")
    assert body["subject"] is None  # empty strings are normalised to null


def test_metadata_includes_form_fields(client):
    body = client.post("/metadata", **upload(make_form_pdf())).json()
    fields = {f["name"]: f for f in body["form_fields"]}
    assert fields["full_name"] == {"name": "full_name", "value": "Ada Lovelace", "pages": [1]}
    assert fields["agree"]["value"] == "Yes"


# -- encrypted documents ----------------------------------------------------


def test_encrypted_pdf_requires_password(client):
    secret = make_pdf(password="hunter2")
    assert_error(client.post("/convert", **upload(secret)), 422, "password_required")
    assert_error(client.post("/convert", **upload(secret, password="wrong")), 422, "invalid_password")
    response = client.post("/convert", **upload(secret, password="hunter2"))
    assert response.status_code == 200
    assert "Chapter 1" in response.text
    info = client.post("/metadata", **upload(secret, password="hunter2")).json()
    assert info["encrypted"] is True


# -- validation and limits --------------------------------------------------


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"pages": "4"}, 422, "invalid_page_range"),
        ({"pages": "2-9"}, 422, "invalid_page_range"),
        ({"pages": "3-1"}, 422, "invalid_request"),
        ({"pages": "abc"}, 422, "invalid_request"),
        ({"format": "docx"}, 422, "invalid_request"),
        ({"ocr_language": "eng;rm"}, 422, "invalid_request"),
        ({"page": "1"}, 422, "invalid_request"),  # unknown parameter, likely a typo
    ],
)
def test_invalid_parameters(client, pdf, params, status, code):
    assert_error(client.post("/convert", params=params, **upload(pdf)), status, code)


def test_blank_pages_parameter_means_all_pages(client, pdf):
    response = client.post("/convert", params={"pages": ""}, **upload(pdf))
    assert response.status_code == 200
    assert "Chapter 3" in response.text


def test_page_limit(client, settings):
    big = make_pdf(pages=settings.max_pages + 1)
    body = assert_error(client.post("/convert", **upload(big)), 422, "too_many_pages")
    assert "pages" in body["detail"]
    # Selecting a subset within the limit works.
    assert client.post("/convert", params={"pages": "1-2"}, **upload(big)).status_code == 200


def test_empty_upload(client):
    assert_error(client.post("/convert", **upload(b"")), 400, "empty_file")


def test_missing_file(client):
    assert_error(client.post("/convert"), 422, "invalid_request")


def test_upload_over_limit_by_content_length(client, settings):
    too_big = b"%PDF-1.7\n" + b"0" * (settings.max_upload_bytes * 3)
    assert_error(client.post("/convert", **upload(too_big)), 413, "file_too_large")


def test_upload_over_limit_within_multipart_slack(client, settings):
    # Passes the coarse body-size check but exceeds the per-file limit.
    slightly_too_big = b"%PDF-1.7\n" + b"0" * settings.max_upload_bytes
    assert_error(client.post("/convert", **upload(slightly_too_big)), 413, "file_too_large")


def test_upload_over_limit_without_content_length(client, settings):
    boundary = "testboundary"

    def body():
        yield (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        chunk = b"0" * (256 * 1024)
        for _ in range(settings.max_upload_bytes * 3 // len(chunk)):
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    response = client.post(
        "/convert", content=body(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert_error(response, 413, "file_too_large")


# -- OCR ----------------------------------------------------------------------


def test_force_ocr_without_engine_is_rejected(client, pdf, ocr_available):
    if ocr_available:
        pytest.skip("an OCR engine is installed")
    assert_error(client.post("/convert", params={"ocr": "force"}, **upload(pdf)), 422, "ocr_unavailable")


def test_unknown_ocr_language_is_rejected(client, pdf, ocr_available):
    if not ocr_available:
        pytest.skip("no OCR engine installed")
    response = client.post("/convert", params={"ocr_language": "klingon"}, **upload(pdf))
    assert "klingon" in assert_error(response, 422, "unsupported_ocr_language")["detail"]


def test_ocr_reads_scanned_image(client, ocr_available):
    if not ocr_available:
        pytest.skip("no OCR engine installed")
    response = client.post("/convert", **upload(make_scanned_png("SCANNED INVOICE 4711"), name="scan.png"))
    assert response.status_code == 200
    assert "INVOICE" in response.text.upper() and "4711" in response.text


def test_ocr_off_skips_scanned_text(client):
    response = client.post("/convert", params={"ocr": "off"}, **upload(make_scanned_png(), name="scan.png"))
    assert response.status_code == 200
    assert "INVOICE" not in response.text


# -- service endpoints ---------------------------------------------------------


def test_health_is_public(client):
    response = client.get("/health", headers=NO_KEY)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["workers"]["size"] == 2


def test_info(client, settings):
    body = client.get("/info").json()
    assert body["workers"] == settings.workers
    assert body["limits"] == {
        "max_upload_mb": settings.max_upload_mb,
        "max_pages": settings.max_pages,
        "processing_timeout_seconds": settings.processing_timeout,
    }
    assert {"pymupdf", "pymupdf4llm"} <= set(body["libraries"])


def test_openapi_documents_the_endpoints(client):
    paths = client.get("/openapi.json", headers=NO_KEY).json()["paths"]
    assert {"/convert", "/extract", "/chunks", "/metadata", "/info", "/health"} <= set(paths)


# -- authentication -------------------------------------------------------------


NO_KEY = {"X-API-Key": ""}  # overrides the test client's default key


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "wrong"}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {API_KEY}"}],
)
def test_requests_without_valid_key_are_rejected(client, pdf, headers):
    response = client.post("/convert", headers=NO_KEY | headers, **upload(pdf))
    body = assert_error(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"
    assert "API key" in body["detail"]


def test_bearer_token_is_accepted(client, pdf):
    response = client.post("/convert", headers=NO_KEY | {"Authorization": f"Bearer {API_KEY}"}, **upload(pdf))
    assert response.status_code == 200


def test_unauthenticated_upload_is_rejected_before_the_body_is_read(client, settings):
    # Far over the size limit: auth must answer first, without reading the body.
    huge = b"0" * (settings.max_upload_bytes * 3)
    assert_error(client.post("/convert", headers=NO_KEY, **upload(huge)), 401, "unauthorized")


def test_auth_is_disabled_without_keys():
    app = Starlette(routes=[Route("/info", lambda request: PlainTextResponse("open"))])
    with TestClient(APIKeyMiddleware(app, keys=())) as open_client:
        assert open_client.get("/info").text == "open"


def test_misconfigured_ocr_fails_at_startup():
    from app.config import Settings
    from app.main import create_app

    # Russian with RapidOCR only can never work; refuse to start rather than fail every request.
    app = create_app(Settings(workers=1, ocr_engine="rapidocr", ocr_language="rus"))
    with pytest.raises(RuntimeError, match="cannot work"):
        with TestClient(app):
            pass
