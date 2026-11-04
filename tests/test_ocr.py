"""OCR engine selection, and the RapidOCR language list checked against its model."""

import pytest

from app.errors import ServiceError
from app.ocr import RAPIDOCR_LANGUAGES, select_engine

BOTH = {"ocr_engines": {"rapidocr": {"languages": sorted(RAPIDOCR_LANGUAGES)}, "tesseract": {"languages": ["eng", "rus"]}}}
RAPID_ONLY = {"ocr_engines": {"rapidocr": {"languages": sorted(RAPIDOCR_LANGUAGES)}}}
TESS_ONLY = {"ocr_engines": {"tesseract": {"languages": ["eng", "rus"]}}}
NONE: dict = {"ocr_engines": {}}


@pytest.mark.parametrize(
    ("choice", "language", "capabilities", "expected"),
    [
        ("auto", "eng", BOTH, "rapidocr"),  # preferred: more robust on poor scans
        ("auto", "deu+fra", BOTH, "rapidocr"),
        ("auto", "rus", BOTH, "tesseract"),  # Cyrillic: only Tesseract can
        ("auto", "eng+rus", BOTH, "tesseract"),
        ("auto", "eng", TESS_ONLY, "tesseract"),
        ("auto", "chi_sim", RAPID_ONLY, "rapidocr"),
        ("tesseract", "eng", BOTH, "tesseract"),
        ("rapidocr", "pol", BOTH, "rapidocr"),
    ],
)
def test_select_engine(choice, language, capabilities, expected):
    assert select_engine(choice, language, capabilities) == expected


@pytest.mark.parametrize(
    ("choice", "language", "capabilities", "code"),
    [
        ("auto", "eng", NONE, "ocr_unavailable"),
        ("auto", "rus", RAPID_ONLY, "unsupported_ocr_language"),
        ("auto", "ara", BOTH, "unsupported_ocr_language"),
        ("rapidocr", "rus", BOTH, "unsupported_ocr_language"),  # would silently produce garbage
        ("rapidocr", "eng", TESS_ONLY, "ocr_engine_unavailable"),
        ("tesseract", "deu", BOTH, "unsupported_ocr_language"),  # deu data not installed
    ],
)
def test_select_engine_errors(choice, language, capabilities, code):
    with pytest.raises(ServiceError) as info:
        select_engine(choice, language, capabilities)
    assert info.value.code == code


# Letters beyond plain A-Z for each language, both cases where they exist.
ALPHABETS = {
    "eng": "", "lat": "", "ind": "", "msa": "", "tgl": "ñÑ", "swa": "",
    # Letters only: the model lacks the punctuation mark "¡", which does not make Spanish unreadable.
    "deu": "äöüßÄÖÜ", "fra": "àâæçéèêëîïôœùûüÿÀÂÆÇÉÈÊËÎÏÔŒÙÛÜŸ", "spa": "áéíñóúüÁÉÍÑÓÚÜ",
    "ita": "àèéìíîòóùúÀÈÉÌÍÎÒÓÙÚ", "por": "ãáâàçéêíõóôúÃÁÂÀÇÉÊÍÕÓÔÚ", "nld": "éëïóöüÉËÏÓÖÜ",
    "cat": "àçèéíïòóúüÀÇÈÉÍÏÒÓÚÜ·", "glg": "áéíñóúÁÉÍÑÓÚ", "eus": "ñÑ",
    "dan": "æøåÆØÅ", "nor": "æøåÆØÅ", "swe": "åäöÅÄÖ", "fin": "äöåšžÄÖÅŠŽ", "isl": "áðéíóúýþæöÁÐÉÍÓÚÝÞÆÖ",
    "pol": "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "ces": "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ", "slk": "áäčďéíĺľňóôŕšťúýžÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ",
    "slv": "čšžČŠŽ", "hrv": "čćđšžČĆĐŠŽ", "bos": "čćđšžČĆĐŠŽ", "srp_latn": "čćđšžČĆĐŠŽ",
    "hun": "áéíóöőúüűÁÉÍÓÖŐÚÜŰ", "ron": "ăâîșțĂÂÎȘȚşţŞŢ", "lit": "ąčęėįšųūžĄČĘĖĮŠŲŪŽ",
    "lav": "āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ", "est": "äöõüšžÄÖÕÜŠŽ", "tur": "çğıİöşüÇĞÖŞÜ", "sqi": "çëÇË",
    "afr": "êëïôûÊËÏÔÛ", "epo": "ĉĝĥĵŝŭĈĜĤĴŜŬ", "cym": "âêîôûŵŷÂÊÎÔÛŴŶ", "gle": "áéíóúÁÉÍÓÚ",
    "mlt": "ċġħżĊĠĦŻ", "aze": "çəğıİöşüÇƏĞÖŞÜ",
    "chi_sim": "的一是在不了有和人这中大为上个国我以要他", "chi_tra": "國這們來時會說對發開關問題實現樣學經",
    "jpn": "あいうえおかきくけこアイウエオカキクケコ日本語",
}  # fmt: skip


def test_language_samples_cover_the_list():
    assert set(ALPHABETS) == RAPIDOCR_LANGUAGES


def test_rapidocr_model_can_write_every_listed_language():
    onnxruntime = pytest.importorskip("onnxruntime")
    rapidocr = pytest.importorskip("rapidocr")
    from pathlib import Path

    model = Path(rapidocr.__file__).parent / "models" / "PP-OCRv6_rec_small.onnx"
    session = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    characters = set(session.get_modelmeta().custom_metadata_map["character"].split("\n"))
    missing = {lang: "".join(c for c in letters if c not in characters) for lang, letters in ALPHABETS.items()}
    assert {lang: m for lang, m in missing.items() if m} == {}
