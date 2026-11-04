FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Opt out of onnxruntime's built-in telemetry (also enforced in app/__init__.py).
ENV ORT_DISABLE_TELEMETRY=1

# Language data for OCR of scanned pages and images. MuPDF has Tesseract
# built in, so only the trained models are needed, not the tesseract binary.
# Space-separated Tesseract codes, e.g. --build-arg OCR_LANGUAGES="eng deu chi_sim".
ARG OCR_LANGUAGES="eng"
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      $(for lang in $OCR_LANGUAGES; do echo "tesseract-ocr-$(echo "$lang" | tr _ -)"; done) \
 && rm -rf /var/lib/apt/lists/* \
 && for lang in $OCR_LANGUAGES; do test -f "$TESSDATA_PREFIX/$lang.traineddata"; done

WORKDIR /app
COPY requirements.lock .
RUN pip install -r requirements.lock
COPY app ./app

RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# One uvicorn process: parallelism comes from the WORKERS process pool.
# Don't add --workers; each uvicorn worker would start a pool of its own.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
