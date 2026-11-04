"""Page selection: parsing specs like ``1-3,5,10-`` and resolving them."""

from __future__ import annotations

import re

PageRanges = tuple[tuple[int, int | None], ...]
"""1-based inclusive ``(start, end)`` pairs; ``end=None`` means "to the last page"."""

_RANGE = re.compile(r"(\d+)(?:\s*(-)\s*(\d*))?")


def parse_page_spec(spec: str) -> PageRanges:
    """Parse a 1-based page spec: ``"1-3,5,10-"`` → ``((1, 3), (5, 5), (10, None))``.

    Raises ``ValueError`` with a user-facing message on malformed input.
    Bounds against the real page count are checked by :func:`resolve_pages`.
    """
    ranges = []
    for part in spec.split(","):
        part = part.strip()
        match = _RANGE.fullmatch(part)
        if not match:
            raise ValueError(f"Invalid page range {part!r}; use e.g. '1-3,5,10-'")
        start = int(match[1])
        if match[2] is None:
            end: int | None = start
        elif match[3]:
            end = int(match[3])
        else:
            end = None
        if start < 1:
            raise ValueError("Page numbers start at 1")
        if end is not None and end < start:
            raise ValueError(f"Invalid page range {part!r}: end is before start")
        ranges.append((start, end))
    return tuple(ranges)


def resolve_pages(ranges: PageRanges, page_count: int) -> list[int]:
    """Turn parsed ranges into sorted, de-duplicated 0-based page indices."""
    pages: set[int] = set()
    for start, end in ranges:
        last = page_count if end is None else end
        if start > page_count or last > page_count:
            raise ValueError(
                f"Page {max(start, last)} is out of range; the document has {page_count} page(s)"
            )
        pages.update(range(start - 1, last))
    return sorted(pages)
