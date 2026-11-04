"""Service configuration, read from environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field


def available_cpus() -> int:
    """CPUs this process may actually use.

    ``os.cpu_count()`` reports the host's CPUs, which overstates capacity in
    containers. Honour both the scheduler affinity mask and a cgroup v2 CPU
    quota (what ``docker run --cpus`` and Kubernetes CPU limits set).
    """
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().split()
        if quota != "max":
            cpus = min(cpus, int(quota) // int(period))
    except (OSError, ValueError):
        pass
    return max(1, cpus)


def _default_workers() -> int:
    # Each worker holds its own copy of the layout models (~250 MB RSS), so
    # cap the default; operators with more RAM can raise WORKERS explicitly.
    return min(4, available_cpus())


@dataclass(frozen=True)
class Settings:
    max_upload_mb: float = 100.0
    """Largest accepted upload, in MiB."""

    max_pages: int = 1000
    """Most pages processed per request (after ``pages`` selection). 0 disables the limit."""

    workers: int = field(default_factory=_default_workers)
    """Number of worker processes, i.e. how many documents are processed in parallel."""

    processing_timeout: float = 300.0
    """Seconds a single document may take before its worker is killed."""

    max_queue: int = 32
    """Requests allowed to wait for a free worker before new ones get a 503."""

    max_tasks_per_worker: int = 200
    """Restart a worker after this many documents to bound memory growth. 0 disables recycling."""

    onnx_threads: int = 1
    """Intra-op threads per worker for the ONNX models (layout analysis, RapidOCR). 0 keeps the ONNX Runtime default."""

    api_keys: tuple[str, ...] = ()
    """Accepted API keys. Empty means authentication is disabled."""

    ocr_language: str = "eng"
    """Default OCR language(s), as Tesseract codes: ``eng``, ``eng+deu``, ..."""

    ocr_engine: str = "auto"
    """Default OCR engine: ``auto`` (RapidOCR where it supports the language, else Tesseract), ``rapidocr`` or ``tesseract``."""

    def __post_init__(self) -> None:
        checks = {
            "max_upload_mb": self.max_upload_mb > 0,
            "max_pages": self.max_pages >= 0,
            "workers": self.workers >= 1,
            "processing_timeout": self.processing_timeout > 0,
            "max_queue": self.max_queue >= 0,
            "max_tasks_per_worker": self.max_tasks_per_worker >= 0,
            "onnx_threads": self.onnx_threads >= 0,
            "ocr_language": re.fullmatch(r"[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*", self.ocr_language) is not None,
            "ocr_engine": self.ocr_engine in ("auto", "rapidocr", "tesseract"),
        }
        invalid = [name for name, ok in checks.items() if not ok]
        if invalid:
            raise ValueError(f"Invalid setting(s): {', '.join(invalid)}")

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        def get(name: str, convert, attr: str):
            raw = env.get(name, "").strip()
            if not raw:
                return {}
            try:
                return {attr: convert(raw)}
            except ValueError:
                raise ValueError(f"Environment variable {name}={raw!r} is not a valid {convert.__name__}") from None

        def keys(raw: str) -> tuple[str, ...]:
            return tuple(k.strip() for k in raw.split(",") if k.strip())

        values = {
            **get("MAX_UPLOAD_MB", float, "max_upload_mb"),
            **get("MAX_PAGES", int, "max_pages"),
            **get("WORKERS", int, "workers"),
            **get("PROCESSING_TIMEOUT_SECONDS", float, "processing_timeout"),
            **get("MAX_QUEUE", int, "max_queue"),
            **get("MAX_TASKS_PER_WORKER", int, "max_tasks_per_worker"),
            **get("ONNX_THREADS", int, "onnx_threads"),
            **get("API_KEYS", keys, "api_keys"),
            **get("OCR_LANGUAGE", str, "ocr_language"),
            **get("OCR_ENGINE", str, "ocr_engine"),
        }
        return cls(**values)
