"""Which pages of a scan can be read as the book's own title page.

OCR text is the only witness to what a scan is, so anything on it that merely *mentions* another
book is dangerous: "Also by John Scalzi: Old Man's War, The Last Colony" names a book beside the
author's name exactly like a title page does. Pages that list books (other titles by the author,
praise, "about the author") are therefore set aside before any matching. Setting text aside can
only remove evidence, never add it.

The unit is the *page*: tesseract separates pages with a form feed, and a list can run to any
length, so nothing short of the rest of the page is safe. A digit read for a letter in the
heading ("ALS0 BY") still counts as the heading.
"""

from __future__ import annotations

import re

PAGE_BREAK = "\f"

_LOOKALIKES = str.maketrans({"0": "o", "1": "l", "5": "s", "|": "l"})
_HEADINGS = re.compile(
    r"\b(?:"
    r"al+so\s+by|also\s+available|by\s+the\s+same\s+author|praise\s+for|about\s+the\s+authors?|"
    r"(?:other|more|further|additional|previous|earlier|recent|selected)\s+(?:books?|titles?|works?|novels?)|"
    r"(?:books?|titles?|novels?|works?)\s+by|look\s+for|books?\s+in\s+the\b"
    r")\b",
    flags=re.IGNORECASE,
)


def _heading_text(line: str) -> str:
    """The line as a heading would be read: lower case, look-alike digits as letters, words only."""
    return re.sub(r"[^a-z]+", " ", line.lower().translate(_LOOKALIKES)).strip()


def without_book_lists(text: str) -> tuple[str, int]:
    """(text, pages_affected): each page cut at its first list heading; pages stay separated by a form feed.

    A heading drops its own line and everything after it on the page. A page that is nothing
    but a list vanishes."""
    kept: list[str] = []
    affected = 0
    for page in (text or "").split(PAGE_BREAK):
        lines = page.split("\n")
        cut = next((i for i, line in enumerate(lines) if _HEADINGS.search(_heading_text(line))), None)
        if cut is not None:
            affected += 1
            lines = lines[:cut]
        body = "\n".join(lines).strip()
        if body:
            kept.append(body)
    return PAGE_BREAK.join(kept), affected
