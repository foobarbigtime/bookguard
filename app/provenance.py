"""Provenance: what Bindery actually downloaded for a book, as independent evidence.

The release name an indexer or uploader wrote ("Andy Griffiths - [Treehouse 10] - The
130-Storey Treehouse (retail) (epub)") is not part of the file and not part of Bindery's
catalogue title, so it is a separate witness to what the file is supposed to be. It proves
nothing alone (a correctly named release can contain the wrong file), but when the release
name, the file's own metadata and the assigned title all name the same book, and the book's
text contains it, three independent sources agree.

Read-only: Bindery's database is opened read-only; nothing is written anywhere.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import sqlite3
from typing import Any, Iterable

from .db import bindery_conn
from .title_matching import canon_tokens, clip, designations, edition_markers, normalize

MAX_RELEASES = 20
_FLAG_WORDS = {
    "retail", "epub", "mobi", "azw", "azw3", "pdf", "kindle", "ebook", "mp3", "m4b", "m4a",
    "unabridged", "abridged", "audiobook", "audio", "web", "scan", "ocr", "proper", "repack",
    "isbn", "v1", "v2", "v3", "true", "digital", "kobo", "retail-epub", "docx", "txt", "rtf", "cbz", "cbr",
}
_MUSIC_WORDS = re.compile(r"\b(?:ost|soundtrack|flac|discography|web-\d{4}|single|ep)\b", re.IGNORECASE)
_SERIES_FORM = re.compile(
    r"^(?P<author>.+?)\s+-\s+\[(?P<series>.+?)\s*(?P<num>\d+(?:\.\d+)?)?\]\s+-\s+(?P<title>.+)$"
)
_PLAIN_FORM = re.compile(r"^(?P<author>[^\[\]]+?)\s+-\s+(?P<title>.+)$")
_TRAILING_GROUP = re.compile(r"\s*\(([^()]*)\)\s*$")
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_APOSTROPHES = str.maketrans("", "", "'’ʼ‘")
_ARTICLES = {"the", "a", "an"}


@dataclass
class Release:
    raw: str
    kind: str  # "book", "music", or "unknown"
    author: str = ""
    series: str = ""
    number: str = ""
    title: str = ""
    event: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _strip_flags(value: str) -> str:
    """Remove trailing "(retail) (epub)"-style groups, keeping real title parentheticals."""
    while True:
        found = _TRAILING_GROUP.search(value)
        if not found:
            return value.strip()
        words = {w for w in re.split(r"[\s,./+-]+", found.group(1).lower()) if w}
        if not words or not words <= _FLAG_WORDS:
            return value.strip()
        value = value[: found.start()]


def parse_release_name(raw: str, event: str = "") -> Release:
    """Parse "Author - [Series NN] - Title (flags)" and "Author - Title (flags)"; else unknown."""
    text = re.sub(r"\s+", " ", clip(raw).strip())
    if not text:
        return Release(raw="", kind="unknown", event=event)
    if _MUSIC_WORDS.search(text.replace("_", " ")) and " - " not in text:
        return Release(raw=text, kind="music", event=event)
    cleaned = _strip_flags(text)
    for pattern in (_SERIES_FORM, _PLAIN_FORM):
        found = pattern.match(cleaned)
        if found:
            groups = found.groupdict()
            author = groups["author"].strip()
            title = groups["title"].strip()
            if 1 < len(author) <= 60 and title:
                return Release(
                    raw=text, kind="book", author=author, series=(groups.get("series") or "").strip(),
                    number=(groups.get("num") or "").lstrip("0") or ("0" if groups.get("num") else ""),
                    title=title, event=event,
                )
    return Release(raw=text, kind="unknown", event=event)


def releases_for_book(book_id: int) -> list[Release]:
    """Download names Bindery recorded for this book (imports first), read-only."""
    if not book_id:
        return []
    try:
        with bindery_conn() as conn:
            rows = conn.execute(
                "SELECT event_type, source_title FROM history "
                "WHERE book_id = ? AND source_title != '' ORDER BY id DESC LIMIT ?",
                (int(book_id), MAX_RELEASES),
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return []
    found: list[Release] = []
    seen: set[str] = set()
    for event, source in sorted(rows, key=lambda r: 0 if "mport" in str(r[0]) else 1):
        if "ail" in str(event) or source in seen:  # downloadFailed / importFailed prove nothing
            continue
        seen.add(source)
        found.append(parse_release_name(source, str(event)))
    return found


def core_title(title: str, author: str = "") -> str:
    """The title with everything that is not the work's own name removed.

    Drops bracketed series labels, an appended author name, a leading or library-style
    trailing article, apostrophes and punctuation. Word order and a subtitle are NOT
    dropped: "The English Girl" and "The English Girl: Spy Stories" stay different.
    """
    tokens = canon_tokens(_BRACKETS.sub(" ", clip(title)))
    author_tokens = normalize(author).split()
    if author_tokens and len(tokens) > len(author_tokens) and tokens[-len(author_tokens):] == author_tokens:
        tokens = tokens[: -len(author_tokens)]
    return " ".join(tokens)


def _same_designations(*titles: str, release_number: str = "") -> bool:
    sets = [d for d in (designations(t) for t in titles) if d]
    if release_number:
        sets.append({release_number})
    return all(s == sets[0] for s in sets[1:]) if sets else True


def provenance_proof(
    expected: dict,
    embedded: dict,
    signals: dict,
    releases: Iterable[Release],
    author_level,
) -> dict[str, Any] | None:
    """Three-way agreement: the download name, the file's own metadata and the assigned title.

    Needs, all together: the same author on all three; the same core title on all three;
    the same edition markers and volume numbers; and the book's own text containing the
    title or the author. Returns the proof (which sources agreed) or None.
    """
    title, author = str(expected.get("title") or ""), str(expected.get("author") or "")
    file_title, file_author = str(embedded.get("title") or ""), str(embedded.get("author") or "")
    file_level = author_level(author, file_author) if (title and author and file_title and file_author) else ""
    if not file_level:
        return None
    if not (signals.get("expectedTitleInText") or signals.get("expectedAuthorInText") or signals.get("fileTitleInText")):
        return None
    assigned_core = core_title(title, author)
    file_core = core_title(file_title, file_author)
    if not assigned_core or assigned_core != file_core:
        return None
    for release in releases:
        release_level = author_level(author, release.author) if release.kind == "book" and release.title else ""
        if not release_level:
            continue
        # An initial for a name, or an omitted middle name, needs the book to print the author.
        if "soft" in (file_level, release_level) and not signals.get("expectedAuthorInText"):
            continue
        if core_title(release.title, release.author) != assigned_core:
            continue
        if not (edition_markers(title) == edition_markers(file_title) == edition_markers(release.title)):
            continue
        if not _same_designations(title, file_title, release.title, release_number=release.number):
            continue
        return {
            "sources": ["download name in Bindery's history", "the file's own title and author", "the book's text"],
            "release": release.raw,
            "coreTitle": assigned_core,
        }
    return None
