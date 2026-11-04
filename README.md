# PDF Text Extraction Service

An HTTP service that extracts text from PDFs, built for search indexing, LLM
and RAG pipelines. It runs on [pymupdf4llm](https://github.com/pymupdf/pymupdf4llm),
which uses a layout model to get reading order, headings, lists and tables right.

- Markdown or plain text, for the whole document or page by page
- Layout-aware **chunking for RAG**, with page numbers, bounding boxes and section paths
- **OCR** of scanned pages and images (Tesseract), applied only where needed
- **Encrypted PDFs**, page ranges, HTML tables, page header/footer removal
- Metadata, outline (bookmarks) and **form-field values**
- Also reads XPS, EPUB, MOBI, FB2, CBZ, SVG and images; the type is detected from the content, not the file name
- Production basics: process isolation, timeouts, upload and page limits,
  back-pressure, optional API keys, consistent JSON errors

## Quick start

```sh
docker build -t pdf-service .
docker run -p 8000:8000 pdf-service

curl -F file=@report.pdf localhost:8000/convert
```

Interactive API docs are served at <http://localhost:8000/docs>.

## API

| Endpoint | Returns |
|---|---|
| `POST /convert` | The text as `text/plain`: Markdown (default) or plain text |
| `POST /extract` | JSON: document metadata plus the text of each page |
| `POST /chunks` | JSON: document metadata plus retrieval-ready chunks |
| `POST /metadata` | JSON: metadata, outline and form fields; fast, no text extraction |
| `GET /info` | Server limits, OCR languages, library versions |
| `GET /health` | Liveness and worker-pool status; never requires an API key |

Document endpoints take a multipart upload in the `file` field. Options go in
the query string. The one exception is `password`, which is a form field so it
stays out of URLs and access logs. Unknown query parameters are rejected, so a
typo like `?page=2` fails loudly instead of being ignored.

### Options

Shared by `/convert`, `/extract` and `/chunks`:

| Parameter | Default | |
|---|---|---|
| `pages` | all | 1-based, inclusive: `1-3,5,10-` (`10-` runs to the last page) |
| `ocr` | `auto` | `auto` OCRs only pages without usable text and keeps existing OCR layers. `force` OCRs every page. `off` never OCRs |
| `ocr_language` | server's `OCR_LANGUAGE` | Tesseract languages, e.g. `eng+deu`. They must be installed (see [OCR](#ocr)) |
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
| 422 | `invalid_document` | Not a readable PDF or supported document |
| 422 | `password_required`, `invalid_password` | Encrypted document |
| 422 | `invalid_page_range` | `pages` is outside the document |
| 422 | `too_many_pages` | More pages than `MAX_PAGES`; process the document in parts with `pages` |
| 422 | `ocr_unavailable`, `unsupported_ocr_language` | OCR was requested but no engine is installed, or the language isn't installed |
| 500 | `processing_failed` | The document crashed or broke the parser; details are in the server log |
| 503 | `server_busy` | All workers are busy and the queue is full; honour `Retry-After` |
| 504 | `processing_timeout` | Processing exceeded `PROCESSING_TIMEOUT_SECONDS` |

## Configuration

Set these as environment variables:

| Variable | Default | |
|---|---|---|
| `WORKERS` | CPUs available, max 4 | Documents processed in parallel. Each worker uses about 250 MB of RAM |
| `MAX_UPLOAD_MB` | `100` | Largest accepted upload |
| `MAX_PAGES` | `1000` | Most pages processed per request (`0` = unlimited) |
| `PROCESSING_TIMEOUT_SECONDS` | `300` | A worker that runs longer than this on one document is killed |
| `MAX_QUEUE` | `32` | Requests that may wait for a free worker; further requests get a 503 |
| `MAX_TASKS_PER_WORKER` | `200` | Restart each worker after this many documents to bound memory growth (`0` = never) |
| `API_KEYS` | *(none)* | Comma-separated keys. When set, every endpoint except `/health` and the API docs requires `X-API-Key: <key>` or `Authorization: Bearer <key>` |
| `OCR_LANGUAGE` | `eng` | Default OCR language(s). The service refuses to start if they aren't installed |
| `ONNX_THREADS` | `1` | Layout-model threads per worker (`0` = ONNX Runtime default). See below |

### OCR

The image includes English OCR data. To add other languages, pass
[Tesseract language codes](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-different-versions.html)
when building:

```sh
docker build --build-arg OCR_LANGUAGES="eng deu fra chi_sim" -t pdf-service .
```

`GET /info` lists the installed languages.

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
- **Security.** The container runs as an unprivileged user. Set `API_KEYS`, or
  put the service behind an authenticating proxy, before exposing it.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.lock -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/uvicorn app.main:app --reload
```

Without Tesseract installed locally, the OCR tests are skipped. CI also runs the
suite inside the Docker image, where they run.

| Module | |
|---|---|
| `app/main.py` | HTTP layer: routes, validation, auth and size-limit middleware |
| `app/pool.py` | Worker-process pool: timeouts, crash recovery, recycling, queueing |
| `app/extraction.py` | Document processing; runs inside the workers |
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
internals: OCR-engine selection, its message buffer, and the ONNX session
options.
