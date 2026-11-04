"""Pure-function tests: page specs, PDF dates, settings, errors."""

import pickle

import pytest

from app.config import Settings
from app.errors import ServiceError
from app.extraction import parse_pdf_date
from app.pages import parse_page_spec, resolve_pages


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1", ((1, 1),)),
        ("1-3", ((1, 3),)),
        ("10-", ((10, None),)),
        (" 1 - 3 , 5 ,7- ", ((1, 3), (5, 5), (7, None))),
        ("3-3", ((3, 3),)),
    ],
)
def test_parse_page_spec(spec, expected):
    assert parse_page_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "0", "0-2", "3-1", "a", "1,,2", "1-2-3", "-3", "1;2", "1.5"])
def test_parse_page_spec_rejects(spec):
    with pytest.raises(ValueError):
        parse_page_spec(spec)


def test_resolve_pages_is_sorted_unique_and_zero_based():
    assert resolve_pages(parse_page_spec("5,1-3,2"), 10) == [0, 1, 2, 4]
    assert resolve_pages(parse_page_spec("8-"), 10) == [7, 8, 9]
    assert resolve_pages(parse_page_spec("10"), 10) == [9]


@pytest.mark.parametrize("spec", ["11", "9-11", "11-"])
def test_resolve_pages_out_of_range(spec):
    with pytest.raises(ValueError, match="out of range; the document has 10 page"):
        resolve_pages(parse_page_spec(spec), 10)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("D:20240131235959+01'00'", "2024-01-31T23:59:59+01:00"),
        ("D:20240131235959-05'30'", "2024-01-31T23:59:59-05:30"),
        ("D:20240131235959Z", "2024-01-31T23:59:59+00:00"),
        ("D:20240131235959Z00'00'", "2024-01-31T23:59:59+00:00"),
        ("D:20240131", "2024-01-31T00:00:00"),
        ("D:2024", "2024-01-01T00:00:00"),
        ("20240131120000", "2024-01-31T12:00:00"),
        ("D:20241341", "D:20241341"),  # month 13: keep the raw value
        ("Tuesday", "Tuesday"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_parse_pdf_date(raw, expected):
    assert parse_pdf_date(raw) == expected


def test_settings_from_env():
    settings = Settings.from_env(
        {
            "MAX_UPLOAD_MB": "2.5",
            "MAX_PAGES": "0",
            "WORKERS": "3",
            "PROCESSING_TIMEOUT_SECONDS": "30",
            "API_KEYS": " one, two ,,",
            "OCR_LANGUAGE": "eng+deu",
            "MAX_QUEUE": "",  # blank means default
        }
    )
    assert settings.max_upload_bytes == int(2.5 * 1024 * 1024)
    assert settings.max_pages == 0
    assert settings.workers == 3
    assert settings.processing_timeout == 30
    assert settings.api_keys == ("one", "two")
    assert settings.ocr_language == "eng+deu"
    assert settings.max_queue == Settings().max_queue


@pytest.mark.parametrize(
    "env",
    [{"WORKERS": "lots"}, {"WORKERS": "0"}, {"MAX_UPLOAD_MB": "-1"}, {"OCR_LANGUAGE": "eng;rm -rf"}],
)
def test_settings_reject_invalid_values(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


def test_default_workers_is_bounded():
    assert 1 <= Settings().workers <= 4


def test_service_error_survives_pickling():
    error = ServiceError(503, "server_busy", "busy", headers={"Retry-After": "5"})
    clone = pickle.loads(pickle.dumps(error))
    assert (clone.status_code, clone.code, clone.detail, clone.headers) == (503, "server_busy", "busy", {"Retry-After": "5"})
