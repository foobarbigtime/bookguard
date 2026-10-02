"""ISBN evidence from a file's identifiers, checked against Bindery's editions.

A file's ISBN is strong evidence of which book it is, but only when it is a
real, checksum-valid ISBN and something else agrees with it. This module
normalises identifiers and looks up, read-only, which Bindery books own each
ISBN; the classifier decides what that evidence is worth.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable

MAX_IDENTIFIERS = 25


def isbn13(raw: Any) -> str | None:
    """Return a checksum-valid ISBN-13 (ISBN-10 converted), or None."""
    value = re.sub(r"[^0-9Xx]", "", str(raw or "")).upper()
    if len(value) == 10 and value[:9].isdigit():
        # "X" (ten) is only valid as the final check character.
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(value))
        if total % 11:
            return None
        body = "978" + value[:9]
        check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(body)) % 10) % 10
        return body + str(check)
    if len(value) == 13 and value.isdigit() and value[:3] in {"978", "979"}:
        if sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(value)) % 10 == 0:
            return value
    return None


def file_isbns(identifiers: Iterable[Any]) -> list[str]:
    """Checksum-valid ISBN-13s from a file's identifiers, in first-seen order."""
    found: list[str] = []
    for identifier in list(identifiers or [])[:MAX_IDENTIFIERS]:
        text = str(identifier or "")
        if text.lower().startswith("asin:"):
            continue
        value = isbn13(text.split(":", 1)[-1] if ":" in text else text)
        if value and value not in found:
            found.append(value)
    return found


def _clean(column: str) -> str:
    return f"replace(replace(upper({column}), '-', ''), ' ', '')"


def isbn_evidence(book_id: int | None, identifiers: Iterable[Any]) -> dict:
    """Which Bindery books own the file's ISBNs, read-only; empty if unavailable."""
    isbns = file_isbns(identifiers)
    evidence: dict = {"isbns": isbns, "expectedMatch": False, "otherOwners": []}
    if not isbns or not book_id:
        return evidence
    candidates = set(isbns) | {f"{v[3:12]}" for v in isbns if v.startswith("978")}
    # Bindery may store ISBN-10s; match both the ISBN-13 and the ISBN-10 body.
    from .db import bindery_conn

    placeholders = ",".join("?" for _ in candidates)
    try:
        with bindery_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT e.book_id, e.isbn_13, e.isbn_10, b.title, a.name AS author
                FROM editions e
                JOIN books b ON b.id = e.book_id
                LEFT JOIN authors a ON a.id = b.author_id
                WHERE {_clean('e.isbn_13')} IN ({placeholders})
                   OR substr({_clean('e.isbn_10')}, 1, 9) IN ({placeholders})
                """,
                [*candidates, *candidates],
            ).fetchall()
            others: dict[int, dict] = {}
            for row in rows:
                owned = {isbn13(row["isbn_13"]), isbn13(row["isbn_10"])} & set(isbns)
                if not owned:
                    continue
                owner = int(row["book_id"])
                if owner == int(book_id):
                    evidence["expectedMatch"] = True
                elif owner not in others and len(others) < 5:
                    # Only ebook files count: an audiobook does not make an
                    # ebook a duplicate.
                    files = conn.execute(
                        "SELECT path, size_bytes FROM book_files "
                        "WHERE book_id = ? AND format = 'ebook' ORDER BY id LIMIT 5",
                        (owner,),
                    ).fetchall()
                    others[owner] = {
                        "bookId": owner,
                        "title": str(row["title"] or ""),
                        "author": str(row["author"] or ""),
                        "hasFile": bool(files),
                        "ebookFiles": [
                            {"path": str(f["path"]), "size": int(f["size_bytes"] or 0)}
                            for f in files
                        ],
                    }
    except (sqlite3.Error, OSError, ValueError):
        return evidence

    evidence["otherOwners"] = list(others.values())
    return evidence
