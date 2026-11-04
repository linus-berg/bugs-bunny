# PDF Text Extraction Service

An HTTP service that extracts text from PDFs and office documents, built for
search indexing, LLM and RAG pipelines. It runs on [pymupdf4llm](https://github.com/pymupdf/pymupdf4llm),
which uses a layout model to get reading order, headings, lists and tables right.

- Markdown or plain text, for the whole document or page by page
- Layout-aware **chunking for RAG**, with page numbers, bounding boxes and section paths
- **Office documents**: Word, PowerPoint, Excel, OpenDocument and RTF (see [Office documents](#office-documents))
- **OCR** of scanned pages and images, applied only where needed, with two engines (see [OCR](#ocr))
- **Encrypted PDFs and Office files**, page ranges, HTML tables, page header/footer removal
- Metadata, outline (bookmarks) and **form-field values**
- Also reads XPS, EPUB, MOBI, FB2, CBZ, SVG and images. The type is detected from the content, not the file name
- Production basics: process isolation, timeouts, upload and page limits,
  back-pressure, optional API keys, consistent JSON errors

## Quick start

```sh
docker build -t pdf-service .
docker run -p 8000:8000 pdf-service

curl -F file=@report.pdf localhost:8000/convert
```

Interactive API docs are served at <http://localhost:8000/docs>.

The image is about 1.15 GB, most of it LibreOffice. `--build-arg OFFICE=false`
builds a 690 MB image without it, which still reads PDFs, images and XLSX files.

## API

| Endpoint | Returns |
|---|---|
| `POST /convert` | The text as `text/plain`: Markdown (default) or plain text |
| `POST /extract` | JSON: document metadata plus the text of each page |
| `POST /chunks` | JSON: document metadata plus retrieval-ready chunks |
| `POST /metadata` | JSON: metadata, outline and form fields; fast, no text extraction |
| `GET /info` | Server limits, OCR engines and languages, office formats, library versions |
| `GET /health` | Liveness and worker-pool status; never requires an API key |

Document endpoints take a multipart upload in the `file` field. Options go in
the query string. The one exception is `password`, which is a form field so it
stays out of URLs and access logs. Unknown query parameters are rejected, so a
typo like `?page=2` fails loudly instead of being ignored.

### Options

Shared by `/convert`, `/extract` and `/chunks`:

| Parameter | Default | |
|---|---|---|
| `pages` | all | 1-based, inclusive: `1-3,5,10-` (`10-` runs to the last page). For spreadsheets, pages are sheets |
| `ocr` | `auto` | `auto` OCRs only pages without usable text and keeps existing OCR layers. `force` OCRs every page. `off` never OCRs |
| `ocr_engine` | server's `OCR_ENGINE` | `auto`, `rapidocr` or `tesseract` (see [OCR](#ocr)) |
| `ocr_language` | server's `OCR_LANGUAGE` | Languages as Tesseract codes, e.g. `eng+deu`. Some engine must support them (see [OCR](#ocr)) |
| `tables` | `markdown` | `html` renders tables as HTML, which copes with merged cells (Markdown output only) |
| `password` *(form field)* | | Password for an encrypted document |

`/convert` and `/extract` also take:

| Parameter | Default | |
|---|---|---|
| `format` | `markdown` | `markdown` or `text` |
| `page_headers`, `page_footers` | `true` | Set to `false` to drop running headers/footers, such as titles and page numbers |

`/chunks` also takes:

| Parameter | Default | |
|---|---|---|
| `max_tokens` | `400` | Target maximum chunk size. Tokens are estimated from character counts |
| `min_tokens` | `120` | Smaller chunks are merged with a neighbour |
| `table_mode` | `preserve` | `isolate` puts each table in its own chunk |
| `headers_footers` | `exclude` | `exclude` drops detected headers and footers. `auto` drops those repeated across pages. `include` keeps them all |
| `tagged` | `false` | Adds `tagged_content`, the chunk text prefixed with its section, page and type, which works well for embeddings |

### Examples

```sh
# Plain text of pages 1-3 and 7 onwards
curl -F file=@report.pdf 'localhost:8000/convert?format=text&pages=1-3,7-'

# Page-by-page Markdown with metadata
curl -F file=@report.pdf localhost:8000/extract

# Chunks for a vector store
curl -F file=@report.pdf 'localhost:8000/chunks?max_tokens=300'

# An encrypted PDF
curl -F file=@secret.pdf -F password=hunter2 localhost:8000/convert

# A scanned document in German
curl -F file=@scan.png 'localhost:8000/convert?ocr_language=deu'

# A Word document, and the second sheet of a workbook
curl -F file=@memo.docx localhost:8000/convert
curl -F file=@sales.xlsx 'localhost:8000/extract?pages=2'
```

<details>
<summary>Example <code>/extract</code> response</summary>

```json
{
  "document": {
    "filename": "report.pdf",
    "format": "PDF 1.7",
    "page_count": 12,
    "encrypted": false,
    "title": "Annual Report",
    "author": "Ada Lovelace",
    "subject": null,
    "keywords": null,
    "creator": null,
    "producer": "LibreOffice 24.2",
    "creation_date": "2024-01-31T12:00:00+00:00",
    "modification_date": null,
    "toc": [{ "level": 1, "title": "Introduction", "page": 1 }],
    "form_fields": []
  },
  "format": "markdown",
  "pages": [{ "page": 1, "content": "# Introduction\n\n..." }]
}
```

</details>

### Errors

Errors are JSON with a human-readable `detail` and a stable `code`:

```json
{ "detail": "The document is encrypted. Provide its password in the 'password' form field.", "code": "password_required" }
```

| Status | `code` | |
|---|---|---|
| 400 | `empty_file` | The upload is empty |
| 401 | `unauthorized` | Missing or wrong API key |
| 413 | `file_too_large` | The upload exceeds `MAX_UPLOAD_MB` |
| 422 | `invalid_request` | A parameter is invalid; `detail` lists the problems |
| 422 | `invalid_document` | Not a readable PDF or supported document, or an office file LibreOffice can't convert |
| 422 | `unsupported_format` | An office format that needs LibreOffice, on an image built without it |
| 422 | `password_required`, `invalid_password` | Encrypted document |
| 422 | `unsupported_encryption` | Encrypted OpenDocument file, or an unknown Office encryption method |
| 422 | `invalid_page_range` | `pages` is outside the document |
| 422 | `too_many_pages` | More pages than `MAX_PAGES`; process the document in parts with `pages` |
| 422 | `spreadsheet_too_large` | More than 1,000,000 non-empty cells; select fewer sheets with `pages` |
| 422 | `ocr_unavailable`, `ocr_engine_unavailable` | OCR was forced but no engine is installed, or the requested engine isn't |
| 422 | `unsupported_ocr_language` | No installed engine reads the requested language |
| 500 | `processing_failed` | The document crashed or broke the parser; details are in the server log |
| 503 | `server_busy` | All workers are busy and the queue is full; honour `Retry-After` |
| 504 | `processing_timeout` | Processing exceeded `PROCESSING_TIMEOUT_SECONDS` |

## Configuration

Set these as environment variables:

| Variable | Default | |
|---|---|---|
| `WORKERS` | CPUs available, max 4 | Documents processed in parallel. Each worker uses about 350 MB of RAM |
| `MAX_UPLOAD_MB` | `100` | Largest accepted upload |
| `MAX_PAGES` | `1000` | Most pages processed per request (`0` = unlimited) |
| `PROCESSING_TIMEOUT_SECONDS` | `300` | A worker that runs longer than this on one document is killed |
| `MAX_QUEUE` | `32` | Requests that may wait for a free worker; further requests get a 503 |
| `MAX_TASKS_PER_WORKER` | `200` | Restart each worker after this many documents to bound memory growth (`0` = never) |
| `API_KEYS` | *(none)* | Comma-separated keys. When set, every endpoint except `/health` and the API docs requires `X-API-Key: <key>` or `Authorization: Bearer <key>` |
| `OCR_ENGINE` | `auto` | Default OCR engine: `auto`, `rapidocr` or `tesseract` |
| `OCR_LANGUAGE` | `eng` | Default OCR language(s). The service refuses to start if the default engine can't read them |
| `ONNX_THREADS` | `1` | ONNX Runtime threads per worker, for the layout model and RapidOCR (`0` = ONNX Runtime default). See below |

### OCR

Two engines are installed. Pick one per request with `ocr_engine`, or per server with `OCR_ENGINE`:

| | RapidOCR | Tesseract |
|---|---|---|
| Poor scans (noisy, skewed, low resolution) | Much better: 74–80% of the text recovered in our test where Tesseract produced garbage | Breaks down |
| Clean scans | As accurate | As accurate and about 3× faster |
| Languages | Latin-script languages, Chinese and Japanese, all at once | Any language whose data is installed |

`auto`, the default, uses RapidOCR when it can read every requested language
and Tesseract otherwise. `ocr_language` matters only for Tesseract.
RapidOCR reads all its languages at once. Asking RapidOCR for a language it
cannot read, such as Russian, is rejected rather than returning garbage.

The image includes English data for Tesseract. For other scripts (Cyrillic,
Greek, Arabic, Hebrew, Indic, Thai, Korean, ...) add
[Tesseract language codes](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-different-versions.html)
when building:

```sh
docker build --build-arg OCR_LANGUAGES="eng rus ara kor" -t pdf-service .
```

`GET /info` lists each engine's languages.

### Office documents

| Formats | How |
|---|---|
| DOCX, DOC, ODT, RTF, PPTX, PPT, ODP | Converted to PDF by LibreOffice, then processed like any PDF: pages, outline from headings, tables, chunks, OCR of embedded scans |
| XLSX, XLS, XLSB, ODS | Read cell by cell; each sheet becomes a page, rendered as a table. Chunks are groups of whole rows that repeat the header row and record their row numbers |

Spreadsheets aren't printed to PDF because that clips each cell to its column
width ("Customer 13" becomes "Customer") and splits wide sheets across pages.
Values are shown as stored, so number formats such as percentages aren't applied.

Password-protected Microsoft Office files (DOCX, XLSX, PPTX, DOC, XLS, PPT) are
decrypted with the `password` form field. Encrypted OpenDocument files are not
supported. Metadata comes from the original file, so `/metadata` reports the
document's real author and dates, not LibreOffice and the moment of conversion.
A Word document's pages are as LibreOffice lays them out, which can differ slightly
from Word.

LibreOffice runs with macros, OLE/DDE and external links disabled. Its stock
settings would load a file linked from a document off the server's disk into
the output, and a test checks that this stays blocked. Each worker has a
private LibreOffice profile, and LibreOffice is killed together with a worker
that times out. Conversion adds about 1.5 s per document.

### Deployment notes

- **Scale with `WORKERS`, not uvicorn `--workers`.** Each uvicorn worker would
  start a pool of its own and multiply memory use.
- **Isolation.** Documents are parsed in separate worker processes, never in
  the web server. A malformed file that hangs or crashes the parser affects only
  its own request. That worker is killed or replaced, and `/health` keeps
  responding throughout.
- **Why `ONNX_THREADS=1`.** By default ONNX Runtime starts one thread per core in
  every worker. On an 8-core machine, one thread per worker processed a document
  just as fast while using 1/7 of the CPU, so workers scale instead of competing
  for cores. If you run a single worker on a large machine, `ONNX_THREADS=2` can
  be slightly faster.
- **Telemetry.** The `onnxruntime` package ships Microsoft telemetry, enabled by
  default. The service turns it off (`ORT_DISABLE_TELEMETRY=1`) so that nothing
  about the documents it processes leaves the server.
- **Uploads** are buffered to the temp directory (`/tmp`) and deleted after each
  request. Uploads over the size limit are rejected while they stream in.
- **Network.** The service needs no outbound access: models and fonts are in the
  image, and it starts and works with `--network none`. If you accept office
  files from untrusted sources, blocking egress adds a layer of defence around
  LibreOffice.
- **Security.** The container runs as an unprivileged user. Set `API_KEYS`, or
  put the service behind an authenticating proxy, before exposing it.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/uvicorn app.main:app --reload
```

Tests that need Tesseract or LibreOffice are skipped when those aren't installed
locally. CI also runs the suite inside the Docker image, where everything is present.

| Module | |
|---|---|
| `app/main.py` | HTTP layer: routes, validation, auth and size-limit middleware |
| `app/pool.py` | Worker-process pool: timeouts, crash recovery, recycling, queueing |
| `app/extraction.py` | Document processing; runs inside the workers |
| `app/office.py` | Office format detection, decryption, properties, hardened LibreOffice |
| `app/spreadsheets.py` | Reading, rendering and chunking spreadsheets |
| `app/ocr.py` | OCR engine selection and the RapidOCR language list |
| `app/pages.py` | Page-range parsing |
| `app/config.py` | Settings from environment variables |

### Updating dependencies

`requirements.txt` lists the direct dependencies. `requirements.lock` pins the
whole tree, so the ML stack (onnxruntime, numpy) can't drift between builds. To
upgrade, edit `requirements.txt`, then regenerate the lock and run the tests:

```sh
python -m venv /tmp/lock && /tmp/lock/bin/pip install -r requirements.txt
(echo '# Generated from requirements.txt; see "Updating dependencies" in README.md.'; \
 /tmp/lock/bin/pip freeze --exclude pip) > requirements.lock
```

Upgrade `pymupdf4llm` deliberately. `app/extraction.py` uses a few of its
internals: its OCR plugins, its message buffer, and the ONNX session options.
When upgrading `rapidocr`, `tests/test_ocr.py` checks the language list in
`app/ocr.py` against the new model's character set.
