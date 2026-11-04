"""OCR engine selection. Pure logic, shared by the web process and the workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from app.errors import ServiceError

Engine = Literal["rapidocr", "tesseract"]
EngineChoice = Literal["auto", "rapidocr", "tesseract"]

ENGINE_PREFERENCE: tuple[Engine, ...] = ("rapidocr", "tesseract")
"""Order tried by ``auto``. RapidOCR is far more robust on poor scans: on a
noisy, skewed, two-column 100 dpi page it recovered ~75% of the text where
Tesseract produced garbage. Tesseract is faster on clean scans and covers
many more scripts."""

RAPIDOCR_LANGUAGES = frozenset(
    {
        # Latin-script languages (Tesseract codes) whose complete alphabet,
        # diacritics included, is in the bundled PP-OCRv6 multilingual model.
        # tests/test_ocr.py checks this against the model's character set.
        "eng", "deu", "fra", "spa", "ita", "por", "nld", "cat", "glg", "eus",
        "dan", "nor", "swe", "fin", "isl", "pol", "ces", "slk", "slv", "hrv",
        "bos", "srp_latn", "hun", "ron", "lit", "lav", "est", "tur", "sqi",
        "lat", "afr", "ind", "msa", "tgl", "swa", "epo", "cym", "gle", "mlt",
        "aze",
        # Chinese and Japanese.
        "chi_sim", "chi_tra", "jpn",
    }
)  # fmt: skip
"""Languages the bundled RapidOCR model can read. RapidOCR itself takes no
language parameter; it recognises all of these at once. Other scripts
(Cyrillic, Greek, Arabic, Hebrew, Indic, Thai, Korean, Vietnamese) need
Tesseract and its language data."""


def engine_languages(engine: str, capabilities: Mapping) -> Sequence[str]:
    info = capabilities.get("ocr_engines", {}).get(engine)
    return info["languages"] if info else ()


def select_engine(choice: EngineChoice, language: str, capabilities: Mapping) -> Engine:
    """Pick the OCR engine for ``language`` (e.g. ``"eng+deu"``) or raise a client error."""
    available = capabilities.get("ocr_engines", {})
    wanted = language.split("+")
    candidates = ENGINE_PREFERENCE if choice == "auto" else (choice,)

    for engine in candidates:
        if engine in available and all(lang in engine_languages(engine, capabilities) for lang in wanted):
            return engine

    if not available:
        raise ServiceError(422, "ocr_unavailable", "OCR was requested but no OCR engine is installed on this server.")
    if choice != "auto" and choice not in available:
        raise ServiceError(
            422,
            "ocr_engine_unavailable",
            f"The {choice} OCR engine is not installed on this server. Available: {', '.join(available)}.",
        )
    supported = {engine: list(engine_languages(engine, capabilities)) for engine in candidates if engine in available}
    missing = sorted({lang for lang in wanted if not any(lang in langs for langs in supported.values())})
    detail = "; ".join(f"{engine}: {', '.join(langs) or 'none'}" for engine, langs in supported.items())
    raise ServiceError(
        422,
        "unsupported_ocr_language",
        f"No OCR engine here can read {'+'.join(missing) or language}. Supported languages, by engine: {detail}.",
    )
