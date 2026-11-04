"""Spreadsheets: read cell values directly and render each sheet as a table.

Runs inside worker processes. Spreadsheets are not converted to PDF: a
printed sheet clips every cell to its column width ("Customer 13" becomes
"Customer") and splits wide sheets across pages, separating columns from
their rows. Reading the cells keeps every value, and each sheet becomes one
"page".
"""

from __future__ import annotations

import hashlib
import html
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

import openpyxl

from app.errors import ServiceError

MAX_CELLS = 1_000_000
"""Non-empty cells read per workbook; beyond this the output is too large to be useful in one response."""

CHARS_PER_TOKEN = 4  # same estimate pymupdf4llm uses without tiktoken

TableFormat = Literal["markdown", "html"]


@dataclass
class Sheet:
    name: str
    rows: list[tuple[int, list[str]]]
    """Non-empty rows as ``(row number in the spreadsheet, cell texts)``; all the same width."""


def load(data: bytes) -> list[Sheet]:
    """Read every worksheet of an XLSX/XLSM workbook."""
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ServiceError(422, "invalid_document", "The spreadsheet could not be read. It may be damaged.") from None
    try:
        sheets, cells = [], 0
        for worksheet in workbook.worksheets:
            # Declared dimensions are often bogus (whole-sheet formatting
            # claims A1:XFD1048576) and read-only mode pads every row to
            # them. Without them only cells that exist are returned.
            worksheet.reset_dimensions()
            rows = []
            for number, values in enumerate(worksheet.iter_rows(values_only=True), start=1):
                texts = [_cell_text(v) for v in values]
                while texts and not texts[-1]:
                    texts.pop()
                if not texts:
                    continue
                cells += sum(1 for t in texts if t)
                if cells > MAX_CELLS:
                    raise ServiceError(
                        422,
                        "spreadsheet_too_large",
                        f"The spreadsheet has more than {MAX_CELLS:,} non-empty cells. "
                        "Use the 'pages' parameter to select fewer sheets.",
                    )
                rows.append((number, texts))
            sheets.append(Sheet(worksheet.title, _rectangular(rows)))
    finally:
        workbook.close()
    return sheets


def render(sheet: Sheet, output: Literal["markdown", "text"], tables: TableFormat = "markdown") -> str:
    if output == "text":
        lines = [sheet.name, *("\t".join(_flat(c) for c in cells) for _, cells in sheet.rows)]
        return "\n".join(lines) + "\n"
    if not sheet.rows:
        return f"## {sheet.name}\n"
    header, *body = (cells for _, cells in sheet.rows)
    return f"## {sheet.name}\n\n{_table(header, body, tables)}\n"


def chunk(
    sheets: list[tuple[int, Sheet]],
    *,
    page_count: int,
    max_tokens: int,
    tables: TableFormat = "markdown",
    tagged: bool = False,
) -> list[dict[str, Any]]:
    """Split sheets into chunks of whole rows, repeating the header row in each.

    Shaped like pymupdf4llm's chunks. Every chunk is self-describing (sheet
    name plus column headers), which is what retrieval needs.
    """
    chunks: list[dict[str, Any]] = []

    def emit(index: int, sheet: Sheet, header_row: int, header: list[str], rows: list[tuple[int, list[str]]]) -> None:
        text = f"## {sheet.name}\n\n{_table(header, [cells for _, cells in rows], tables)}"
        numbers = [n for n, _ in rows] or [header_row]
        entry: dict[str, Any] = {
            "id": f"c{len(chunks)}",
            "text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "metadata": {
                "page_start": index + 1,
                "page_end": index + 1,
                "sheet": sheet.name,
                "header_row": header_row,
                "row_start": numbers[0],
                "row_end": numbers[-1],
                "types": ["table"],
                "section_path": [sheet.name],
                "token_count": _tokens(text),
                "ocr": False,
                "page_count": page_count,
            },
        }
        if tagged:
            entry["tagged_content"] = f"[Section] {sheet.name}\n[Page] {index + 1}\n[Type] table\n[Markdown]\n{text}"
        chunks.append(entry)

    for index, sheet in sheets:
        if not sheet.rows:
            continue
        (header_row, header), *body = sheet.rows
        overhead = _tokens(f"## {sheet.name}\n\n{_table(header, [], tables)}")
        group: list[tuple[int, list[str]]] = []
        size = overhead
        for number, cells in body:
            row_tokens = _tokens(_table_row(cells, tables)) + 1
            if group and size + row_tokens > max_tokens:
                emit(index, sheet, header_row, header, group)
                group, size = [], overhead
            group.append((number, cells))
            size += row_tokens
        if group or not body:
            emit(index, sheet, header_row, header, group)
    return chunks


# --------------------------------------------------------------------------


def _rectangular(rows: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
    """Pad rows to a common width and drop columns that are empty in every row on the left."""
    if not rows:
        return rows
    width = max(len(cells) for _, cells in rows)
    lead = min(next(i for i, c in enumerate(cells) if c) for _, cells in rows)
    return [(n, (cells + [""] * (width - len(cells)))[lead:]) for n, cells in rows]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else format(value, ".15g")
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    return str(value).strip()


def _flat(text: str) -> str:
    return " ".join(text.split())


def _table(header: list[str], body: list[list[str]], tables: TableFormat) -> str:
    if tables == "html":
        head = "".join(f"<th>{_html_cell(c)}</th>" for c in header)
        rows = "".join(_table_row(cells, "html") for cells in body)
        return f"<table><tr>{head}</tr>{rows}</table>"
    lines = [_table_row(header, "markdown"), "|" + "---|" * len(header)]
    lines += (_table_row(cells, "markdown") for cells in body)
    return "\n".join(lines)


def _table_row(cells: list[str], tables: TableFormat) -> str:
    if tables == "html":
        return "<tr>" + "".join(f"<td>{_html_cell(c)}</td>" for c in cells) + "</tr>"
    return "|" + "|".join(c.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>") for c in cells) + "|"


def _html_cell(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def _tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)
