"""Second-pass identity evidence for books the first pass could not decide.

The first pass compares the file with the Bindery book it is assigned to and
needs an exact, well-positioned match. When that is not enough, this pass looks
for more evidence instead of giving up:

1. Tolerant identity: the file's own title and author are compared with the
   assigned book allowing for initials, "Surname, Given" order and
   extra credited authors. A tolerant match counts only when the book's text
   also contains the assigned title or author.
2. Catalogue search: the file's own title and author are searched for among the
   other books in Bindery. If exactly one other book matches and the book's text
   supports the file's own identity, the file is that other book.

Read-only: nothing is written to Bindery or to any file. Every check that ran
is recorded in the evidence, so the operator can see what was tried.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import bindery_conn
from .matcher import author_match_strict
from .series_titles import ebook_title_conflict, expected_title_variants
from .title_matching import meaningful_words, normalize

MAX_CANDIDATES = 5
# Bump when the second pass gets stricter, so earlier second-pass verdicts are re-examined.
SECOND_PASS_VERSION = 2
TOLERANT_CONFIDENCE = 88
ISBN_CONFIDENCE = 95
CATALOGUE_CONFIDENCE = 95

_PEOPLE = re.compile(r"\s*(?:;|&|\||\band\b|\bwith\b)\s*", re.IGNORECASE)
_GENERIC_SUFFIX = re.compile(r"\s*[:;,\-\u2013\u2014]\s*(?:a\s+)?(?:novel|memoir|thriller)\s*$", re.IGNORECASE)


_EDITION_MARKERS = {
    "graphic": r"\bgraphic\b", "illustrated": r"\billustrated\b", "abridged": r"\babridged\b",
    "unabridged": r"\bunabridged\b", "adaptation": r"\badaptation\b", "comic": r"\bcomics?\b",
    "manga": r"\bmanga\b", "annotated": r"\bannotated\b", "collection": r"\bcollection\b",
    "omnibus": r"\bomnibus\b", "anthology": r"\banthology\b", "boxset": r"\bbox(?:ed)? set\b",
    "screenplay": r"\bscreenplay\b",
}


def edition_markers(title: str) -> frozenset[str]:
    """Words that make a title a different form of the same story (graphic novel, abridged, ...)."""
    text = normalize(title)
    return frozenset(name for name, pattern in _EDITION_MARKERS.items() if re.search(pattern, text))


def _title_equivalent(expected: str, observed: str) -> bool:
    e, o = normalize(expected), normalize(observed)
    if not e or not o:
        return False
    return e == o or meaningful_words(e) == meaningful_words(o)


_SERIES_PAREN = re.compile(
    r"\s*\(\s*(?P<label>[^()]*?[A-Za-z][^()]*?)\s*(?:[,:#\-]\s*)?"
    r"(?:(?:book|bk|vol\.?|volume|no\.?|number)\s*)?(?P<num>\d+)?\s*\)\s*$",
    re.IGNORECASE,
)


def series_free_title(value: str, allow_unnumbered: bool = False) -> tuple[str, str]:
    """(title without a trailing "(Series, N)" label, the volume number it carried).

    Used even when Bindery lists no series for the book. A bracket that names an
    edition (graphic novel, abridged, ...) is part of the title and is kept. An
    unnumbered bracket such as "(Vindico)" proves nothing by itself, so it is
    stripped only when the caller has other evidence (a matching ISBN).
    """
    value = str(value or "")
    found = _SERIES_PAREN.search(value)
    if not found or edition_markers(found.group("label")):
        return value, ""
    if not found.group("num") and not allow_unnumbered:
        return value, ""
    base = value[: found.start()].strip()
    if not meaningful_words(base):
        return value, ""
    return base, found.group("num") or ""


def _without_generic_suffix(value: str) -> str:
    return _GENERIC_SUFFIX.sub("", str(value or ""))


def soft_title_match(expected: str, observed: str, series_names=(), allow_unnumbered: bool = False) -> bool:
    """Same title, ignoring only a series label (even one Bindery does not list), a leading
    article or a generic "A Novel".

    A substantive subtitle is never ignored: "The English Girl" and "The English
    Girl: Spy Stories" can be different works, and so can two volumes of a saga.
    """
    if not expected or not observed or ebook_title_conflict(expected, observed):
        return False
    # A graphic novel, abridged or illustrated form is a different book from the plain
    # one even when the title words match, so the markers must agree on both sides.
    if edition_markers(expected) != edition_markers(observed):
        return False
    for e in expected_title_variants(expected, series_names) or [expected]:
        for o in expected_title_variants(observed, series_names) or [observed]:
            if _title_equivalent(_without_generic_suffix(e), _without_generic_suffix(o)):
                return True
            (base_e, num_e), (base_o, num_o) = (
                series_free_title(e, allow_unnumbered), series_free_title(o, allow_unnumbered))
            if (base_e, base_o) != (e, o) and not (num_e and num_o and num_e != num_o):
                if _title_equivalent(_without_generic_suffix(base_e), _without_generic_suffix(base_o)):
                    return True
    return False


def _people(value: str) -> list[str]:
    value = str(value or "")
    return [part.strip() for part in _PEOPLE.split(value) if part.strip()] or ([value] if value else [])


def soft_author_match(expected: str, credits: str) -> bool:
    """Same person: surname plus a matching given name or initial, in any order.

    A surname alone is never enough.
    """
    for person in _people(expected):
        tokens = normalize(person).split()
        if len(tokens) < 2 or len(tokens[-1]) < 3:
            continue
        surname, first = tokens[-1], tokens[0]
        for credited in _people(credits):
            others = [t for t in normalize(credited).split() if t != surname]
            if surname not in normalize(credited).split():
                continue
            if any(
                t == first or (len(t) == 1 and t == first[0]) or (len(first) == 1 and t[0] == first)
                for t in others
            ):
                return True
    return False


def same_author(expected: str, credits: str) -> bool:
    return bool(expected and credits) and (
        author_match_strict(expected, credits) or soft_author_match(expected, credits)
    )


def catalogue_candidates(title: str, author: str, exclude_book_id: int) -> list[dict[str, Any]]:
    """Other Bindery books whose title and author match the given identity."""
    words = sorted(meaningful_words(title), key=len, reverse=True)
    if not words:
        return []
    found: list[dict[str, Any]] = []
    with bindery_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.title, a.name AS author
            FROM books b LEFT JOIN authors a ON a.id = b.author_id
            WHERE lower(b.title) LIKE ? AND b.id != ?
            """,
            (f"%{words[0]}%", int(exclude_book_id or 0)),
        ).fetchall()
        for row in rows:
            if not soft_title_match(str(row["title"] or ""), title) or not same_author(str(row["author"] or ""), author):
                continue
            files = conn.execute(
                "SELECT path, size_bytes FROM book_files WHERE book_id = ? AND format = 'ebook' ORDER BY id LIMIT 5",
                (int(row["id"]),),
            ).fetchall()
            found.append({
                "bookId": int(row["id"]),
                "title": str(row["title"] or ""),
                "author": str(row["author"] or ""),
                "hasFile": bool(files),
                "ebookFiles": [{"path": str(f["path"]), "size": int(f["size_bytes"] or 0)} for f in files],
            })
            if len(found) >= MAX_CANDIDATES:
                break
    return found


def second_pass(result: dict, evidence: dict) -> tuple[str, int] | None:
    """(verdict, confidence) if more evidence settles the book; always records what was tried."""
    expected = evidence.get("expected") or {}
    embedded = evidence.get("embedded") or {}
    content = evidence.get("content") or {}
    expected_signal = content.get("expected_signal") or {}
    embedded_signal = content.get("embedded_signal") or {}
    title, author = str(expected.get("title") or ""), str(expected.get("author") or "")
    file_title, file_author = str(embedded.get("title") or ""), str(embedded.get("author") or "")
    series = [str(n) for n in (result.get("series") or [])]
    tried: list[dict[str, Any]] = []
    evidence["secondPass"] = tried
    evidence["secondPassVersion"] = SECOND_PASS_VERSION

    if file_title and file_author:
        isbn_is_assigned_book = bool((evidence.get("isbn") or {}).get("expectedMatch"))
        title_ok = soft_title_match(title, file_title, series, allow_unnumbered=isbn_is_assigned_book)
        author_ok = same_author(author, file_author)
        in_text = bool(expected_signal.get("title_found") or expected_signal.get("author_found"))
        tried.append({"check": "tolerantIdentity", "titleMatches": title_ok,
                      "authorMatches": author_ok, "foundInBookText": in_text})
        if title_ok and author_ok and in_text:
            evidence["explanation"] = (
                "The file's own title and author match this book once initials, name order and "
                "extra credited authors are allowed for, and the book's text contains the title or author."
            )
            if isbn_is_assigned_book:
                evidence["explanation"] += " The file's ISBN is also one of this book's editions in Bindery."
            return "VERIFIED_CORRECT", ISBN_CONFIDENCE if isbn_is_assigned_book else TOLERANT_CONFIDENCE

        expected_in_text = bool(expected_signal.get("title_found") and expected_signal.get("author_found"))
        # The file's ISBN belongs to the assigned book: the first pass already
        # sent conflicting evidence back for review, so it is not overridden here.
        isbn_supports_assigned = bool((evidence.get("isbn") or {}).get("expectedMatch"))
        if (embedded_signal.get("title_found") and not expected_in_text and not (title_ok and author_ok)
                and not isbn_supports_assigned):
            try:
                candidates = catalogue_candidates(file_title, file_author, int(result.get("book_id") or 0))
            except (sqlite3.Error, OSError, ValueError):
                candidates = None
            if candidates is not None:
                tried.append({"check": "catalogueSearch", "matches": len(candidates)})
            if candidates and len(candidates) == 1:
                evidence["actualBook"] = candidates[0]
                evidence["explanation"] = (
                    f"The file's own title and author match \"{candidates[0]['title']}\" "
                    f"(Bindery book {candidates[0]['bookId']}), the book's text supports that "
                    "identity, and the assigned book is not found in it."
                )
                return "WRONG_CONTENT", CATALOGUE_CONFIDENCE
    else:
        tried.append({"check": "fileHasNoTitleOrAuthor"})
    return None
