"""Reading spreadsheets cell by cell, rendering and chunking them."""

import datetime
import io
import re
import time
import zipfile

import openpyxl
import pytest

from app import spreadsheets
from app.errors import ServiceError
from tests.conftest import make_xlsx


def workbook_bytes(fill) -> bytes:
    workbook = openpyxl.Workbook()
    fill(workbook)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_keeps_full_cell_values_and_real_row_numbers():
    def fill(wb):
        ws = wb.active
        ws["B3"], ws["C3"] = "Name", "Note"
        ws["B5"], ws["C5"] = "A customer name far wider than its column", "pipe | and\nnewline"
        ws["B6"], ws["C6"] = 0.1 + 0.2, 3.0

    (sheet,) = spreadsheets.load(workbook_bytes(fill))
    assert sheet.rows == [
        (3, ["Name", "Note"]),
        (5, ["A customer name far wider than its column", "pipe | and\nnewline"]),
        (6, ["0.3", "3"]),
    ]
    assert spreadsheets.render(sheet, "markdown") == (
        "## Sheet\n\n|Name|Note|\n|---|---|\n"
        "|A customer name far wider than its column|pipe \\| and<br>newline|\n|0.3|3|\n"
    )
    assert spreadsheets.render(sheet, "text") == (
        "Sheet\nName\tNote\nA customer name far wider than its column\tpipe | and newline\n0.3\t3\n"
    )


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (True, "TRUE"),
        (42, "42"),
        (1e20, "1e+20"),
        (2.5, "2.5"),
        (datetime.datetime(2026, 3, 1), "2026-03-01"),
        (datetime.datetime(2026, 3, 1, 14, 5), "2026-03-01 14:05:00"),
        (datetime.time(9, 30), "09:30:00"),
        ("  padded  ", "padded"),
    ],
)
def test_cell_text(value, text):
    assert spreadsheets._cell_text(value) == text


def test_html_tables():
    (sheet, _) = spreadsheets.load(make_xlsx())
    html = spreadsheets.render(sheet, "markdown", tables="html")
    assert html.startswith("## Orders\n\n<table><tr><th>Order</th>")
    assert "<td>Customer number 13</td>" in html


def test_empty_sheet():
    (sheet,) = spreadsheets.load(workbook_bytes(lambda wb: None))
    assert sheet.rows == []
    assert spreadsheets.render(sheet, "markdown") == "## Sheet\n"
    assert spreadsheets.chunk([(0, sheet)], page_count=1, max_tokens=100) == []


def test_chunks_repeat_the_header_and_respect_the_budget():
    sheets = spreadsheets.load(make_xlsx())
    chunks = spreadsheets.chunk(list(enumerate(sheets)), page_count=2, max_tokens=120, tagged=True)
    orders = [c for c in chunks if c["metadata"]["sheet"] == "Orders"]
    assert len(orders) > 2
    for c in orders:
        assert c["text"].startswith("## Orders\n\n|Order|Customer|Region|Total|\n|---|")
        assert c["metadata"]["token_count"] <= 120
        assert c["metadata"]["header_row"] == 1
        assert c["tagged_content"].startswith("[Section] Orders\n[Page] 1\n[Type] table")
    # Rows are contiguous and complete: 2..41 across the chunks.
    covered = [n for c in orders for n in range(c["metadata"]["row_start"], c["metadata"]["row_end"] + 1)]
    assert covered == list(range(2, 42))
    assert [c["id"] for c in chunks] == [f"c{i}" for i in range(len(chunks))]
    summary = chunks[-1]
    assert summary["metadata"]["page_start"] == 2 and "|Orders|40|" in summary["text"]


def test_too_many_cells(monkeypatch):
    monkeypatch.setattr(spreadsheets, "MAX_CELLS", 50)
    with pytest.raises(ServiceError) as info:
        spreadsheets.load(make_xlsx())
    assert info.value.code == "spreadsheet_too_large"


def test_bogus_dimensions_do_not_blow_up():
    # Formatting a whole sheet makes Excel declare A1:XFD1048576; read-only
    # openpyxl would pad every row to 16,384 columns.
    source = zipfile.ZipFile(io.BytesIO(make_xlsx()))
    patched = io.BytesIO()
    with zipfile.ZipFile(patched, "w") as out:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "xl/worksheets/sheet1.xml":
                data, count = re.subn(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:XFD1048576"', data)
                assert count == 1
            out.writestr(item, data)
    started = time.perf_counter()
    orders, _ = spreadsheets.load(patched.getvalue())
    assert time.perf_counter() - started < 2
    assert len(orders.rows) == 41 and len(orders.rows[0][1]) == 4


def test_unreadable_workbook():
    with pytest.raises(ServiceError) as info:
        spreadsheets.load(b"PK\x03\x04 not a workbook")
    assert info.value.code == "invalid_document"
