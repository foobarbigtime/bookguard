"""Which pages of a scan can be read as the book's own title page.

OCR text is the only witness to what a scan is, so anything on it that merely *mentions* another
book is dangerous: "Also by John Scalzi: Old Man's War, The Last Colony" names a book beside the
author's name exactly like a title page does. Pages that list books (other titles by the author,
praise, "about the author") are therefore set aside before any matching. Setting text aside can
only remove evidence, never add it.

This is one of two defences. The other is structural and does not depend on recognising a
heading: a scan is only ever identified by the title it *opens* with (see verification_engine), so
a promotion, a contents page or a series list further in cannot make it another book. Because of
that, the heading list here is deliberately short and errs towards not touching real title pages
("A Novel by ...", "Book 3 in the ... series" are title-page lines, not headings).

The unit is the *page*: tesseract separates pages with a form feed, and a list can run to any
length, so nothing short of the rest of the page is safe. Headings are read tolerantly: OCR drops
word spaces ("ALSOBY"), tracks capitals ("A L S O  B Y"), stacks words on separate lines, and reads
a digit for a letter ("ALS0 BY").
"""

from __future__ import annotations

import re

PAGE_BREAK = "\f"

_LOOKALIKES = str.maketrans({"0": "o", "1": "l", "5": "s", "|": "l"})
_HEADINGS = (
    "alsoby", "alsofrom", "alsoavailable", "alsoin", "bythesameauthor", "fromthesameauthor",
    "praisefor", "acclaimfor", "advancepraise", "abouttheauthor",
    "otherbooks", "othertitles", "otherworks", "othernovels", "morebooks", "moretitles", "moreby", "morefrom",
    "furtherbooks", "additionalbooks", "previousbooks", "earlierbooks", "recentbooks", "selectedbooks",
    "booksby", "titlesby",
)
_SHORT_LINE = 40        # a heading is a short line; in longer text it must start the line
_STACKED_FIRST_LINE = 12


def _letters(line: str) -> str:
    """The line's letters only, look-alike digits read as letters: how a heading is compared."""
    return re.sub(r"[^a-z]+", "", line.lower().translate(_LOOKALIKES))


def _is_heading(line: str, next_line: str = "") -> bool:
    letters = _letters(line)
    if not letters:
        return False
    if len(letters) > _SHORT_LINE:                  # running text: only a line that starts as a heading
        return any(letters.startswith(heading) for heading in _HEADINGS)
    forms = [letters]
    # "ALSO" over "BY JOHN SCALZI": only when the first line is itself the start of a heading,
    # so that "JOHN SCALZI" above "ALSO BY ..." does not pull the cut a line early.
    if 3 <= len(letters) <= _STACKED_FIRST_LINE and any(heading.startswith(letters) for heading in _HEADINGS):
        forms.append(letters + _letters(next_line)[:_SHORT_LINE])
    return any(heading in form for form in forms for heading in _HEADINGS)


def without_book_lists(text: str) -> tuple[str, int]:
    """(text, pages_affected): each page cut at its first list heading; pages stay separated by a form feed.

    A heading drops its own line and everything after it on the page. A page that is nothing
    but a list vanishes."""
    kept: list[str] = []
    affected = 0
    for page in (text or "").split(PAGE_BREAK):
        lines = page.split("\n")
        cut = next((i for i, line in enumerate(lines)
                    if _is_heading(line, lines[i + 1] if i + 1 < len(lines) else "")), None)
        if cut is not None:
            affected += 1
            lines = lines[:cut]
        body = "\n".join(lines).strip()
        if body:
            kept.append(body)
    return PAGE_BREAK.join(kept), affected
