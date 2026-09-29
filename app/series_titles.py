"""Recognise series designations that catalogues bake into a book title.

Bindery titles often carry the series inside the title itself:
"Mary, Mary: Alex Cross, Book 11" or "The Hardy Boys 24 # The Short Wave
Mystery", while the book's own title page and metadata say only "Mary, Mary" or
"The Short Wave Mystery". These helpers derive the series-free title as an
*additional* candidate for identity matching. They never relax any other
requirement: the verifier still needs the exact author, agreeing metadata, and
front-of-book evidence before it accepts a book.

A designation is removed only when it is unambiguous:

- a trailing part after ":" or inside trailing brackets that carries a series
  number ("Book 11", "#2", "Vol. 3") or equals a series Bindery records for the
  book, optionally with its number;
- a leading part that is a series Bindery records for the book, optionally
  followed by its number;
- a leading "<words> <number> #" or "<words> <number>:" prefix.

A real subtitle without a number ("Fear Itself: The Horror Fiction of Stephen
King") is never removed on pattern alone.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable

from .matcher import meaningful_words, normalize

_NUMBER = r"\d+(?:\.\d+)?"
_KEYWORD = r"(?:book|bk|volume|vol|no|number|part)"
_TRAILING_DESIGNATION = re.compile(
    rf"(?:^|[\s,;])(?:{_KEYWORD}\.?\s*#?\s*{_NUMBER}|#\s*{_NUMBER})\s*$",
    flags=re.IGNORECASE,
)
_TRAILING_PART = re.compile(r"^(?P<base>.+?)\s*(?::|\(|\[)\s*(?P<tail>[^:()\[\]]+?)\s*[)\]]?\s*$")
_NUMBERED_PREFIX = re.compile(rf"^(?P<series>[^#:]+?)\s+{_NUMBER}\s*[#:]\s*(?P<base>.+)$")
_LEADING_NUMBER = re.compile(rf"^(?:{_KEYWORD}\s+)?{_NUMBER}\s+")
_ARTICLES = {"a", "an", "the"}


def _without_article(normalized: str) -> str:
    words = normalized.split()
    if words and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(words)


def _series_keys(series_names: Iterable[str]) -> list[str]:
    keys = []
    for name in series_names:
        key = _without_article(normalize(name))
        if key and key not in keys:
            keys.append(key)
    # Longest first, so "hardy boys casefiles" wins over "hardy boys".
    return sorted(keys, key=len, reverse=True)


def _usable(base: str, full: str, series_keys: list[str]) -> bool:
    normalized = normalize(base)
    if not normalized or normalized == normalize(full):
        return False
    if _without_article(normalized) in series_keys:
        return False
    return bool(meaningful_words(normalized))


def expected_title_variants(title: str, series_names: Iterable[str] = ()) -> list[str]:
    """Return the full title first, then any unambiguous series-free titles."""
    full = str(title or "").strip()
    if not full:
        return []
    keys = _series_keys(series_names)
    variants = [full]

    def add(candidate: str) -> None:
        candidate = candidate.strip(" -\u2013\u2014:;,#")
        if _usable(candidate, full, keys) and normalize(candidate) not in {
            normalize(v) for v in variants
        }:
            variants.append(candidate)

    trailing = _TRAILING_PART.match(full)
    if trailing:
        tail = trailing.group("tail").strip()
        tail_key = _without_article(normalize(re.sub(rf"[\s,#]*{_NUMBER}\s*$", "", tail)))
        if _TRAILING_DESIGNATION.search(tail) or (tail_key and tail_key in keys):
            add(trailing.group("base"))

    normalized_full = _without_article(normalize(full))
    for key in keys:
        if normalized_full.startswith(key + " "):
            remainder = normalized_full[len(key):].strip()
            add(_LEADING_NUMBER.sub("", remainder))
            break

    numbered = _NUMBERED_PREFIX.match(full)
    if numbered:
        add(numbered.group("base"))

    return variants


def bindery_series_names(book_id: int | None) -> list[str]:
    """Series titles Bindery records for one book, read-only; [] if unavailable."""
    if not book_id:
        return []
    # Imported here so the pure title helpers stay usable without a database.
    from .db import bindery_conn

    try:
        with bindery_conn() as conn:
            rows = conn.execute(
                """
                SELECT s.title
                FROM series_books sb
                JOIN series s ON s.id = sb.series_id
                WHERE sb.book_id = ?
                ORDER BY sb.primary_series DESC, s.title
                """,
                (int(book_id),),
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return []
    return [str(row["title"]) for row in rows if str(row["title"] or "").strip()]
