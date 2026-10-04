"""Books Bindery lists twice, and hiding the empty extra entries.

Bindery's duplicate check ignores case and spacing but not a leading article or
small punctuation, so "Nightingale" and "The Nightingale", or "Treasure Fever"
and "Treasure Fever!", become two books by the same author. The empty one stays
Wanted (Bindery keeps searching for it) and BookGuard can't tell which entry a
file belongs to.

Each group is one of:

* EMPTY_EXTRA: some entries have files and some have none. The empty ones can
  be hidden: Bindery's own "exclude" (it keeps the entry, stops searching for
  it and doesn't add it again; it can be included again on the book's page).
* SPLIT: the ebook is on one entry and the audiobook on the other.
* TWO_COPIES: two entries each have a file of the same kind.
* ALL_EMPTY: no entry has a file.

Only EMPTY_EXTRA entries are ever changed, one at a time, each checked again
against Bindery's database right before. Nothing on disk is touched.
"""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any
import unicodedata

from .actions import ActionError, resolve_bindery_api_key
from .bindery_client import BinderyClient, BinderyClientError
from .config import settings
from .db import bindery_conn, local_conn, utc_now

KINDS = {
    "EMPTY_EXTRA": "An extra entry with no files",
    "SPLIT": "Ebook and audiobook on different entries",
    "TWO_COPIES": "Two copies of the same kind",
    "ALL_EMPTY": "No files on any entry",
}
ARTICLE = re.compile(r"^(the|a|an) ")


def title_key(title: str) -> str:
    """One key for spellings of the same title: case, accents, punctuation and a
    leading article never matter. Words in brackets do ("Book 1" vs "Book 2")."""
    text = unicodedata.normalize("NFKD", str(title or "")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['`]", "", text).replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return ARTICLE.sub("", text).strip()


def _year(value: Any) -> int | None:
    found = re.match(r"\s*(\d{4})", str(value or ""))
    return int(found.group(1)) if found else None


def _entries(conn, book_ids: list[int] | None = None) -> list[dict]:
    """Every book Bindery shows (not excluded), with its files by format."""
    where = "WHERE b.excluded = 0"
    args: list[Any] = []
    if book_ids:
        where += f" AND b.author_id IN (SELECT author_id FROM books WHERE id IN ({','.join('?' * len(book_ids))}))"
        args = [int(i) for i in book_ids]
    books = conn.execute(
        f"""SELECT b.id, b.author_id, b.title, b.release_date, b.ebook_file_path, b.audiobook_file_path,
                   b.file_path, a.name AS author
            FROM books b JOIN authors a ON a.id = b.author_id {where}""",
        args,
    ).fetchall()
    files: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in conn.execute("SELECT book_id, format, path FROM book_files"):
        files[int(row["book_id"])][str(row["format"])].append(str(row["path"]))
    entries = []
    for row in books:
        found = files.get(int(row["id"]), {})
        ebooks = list(found.get("ebook", [])) or [p for p in [row["ebook_file_path"]] if p]
        audio = list(found.get("audiobook", [])) or [p for p in [row["audiobook_file_path"]] if p]
        legacy = [p for p in [row["file_path"]] if p and p not in ebooks + audio]
        entries.append({
            "id": int(row["id"]), "authorId": int(row["author_id"]), "author": str(row["author"]),
            "title": str(row["title"]), "year": _year(row["release_date"]),
            "ebooks": ebooks, "audiobooks": audio, "other": legacy,
        })
    return entries


def _has_files(entry: dict) -> bool:
    return bool(entry["ebooks"] or entry["audiobooks"] or entry["other"])


def _classify(entries: list[dict]) -> dict:
    with_files = [e for e in entries if _has_files(e)]
    empty = [e for e in entries if not _has_files(e)]
    years = {e["year"] for e in entries if e["year"]}
    # Different publication years can mean different books with one title: never hide those.
    years_agree = not years or max(years) - min(years) <= 1
    if with_files and empty:
        kind = "EMPTY_EXTRA"
    elif not with_files:
        kind = "ALL_EMPTY"
    elif sum(1 for e in with_files if e["ebooks"]) > 1 or sum(1 for e in with_files if e["audiobooks"]) > 1:
        kind = "TWO_COPIES"
    else:
        kind = "SPLIT"
    hideable = [e["id"] for e in empty] if kind == "EMPTY_EXTRA" and years_agree else []
    return {"kind": kind, "label": KINDS[kind], "hideable": hideable, "yearsAgree": years_agree}


def find_duplicates(book_ids: list[int] | None = None) -> list[dict]:
    """Groups of two or more visible books by one author whose titles share a key."""
    with bindery_conn() as conn:
        entries = _entries(conn, book_ids)
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for entry in entries:
        key = title_key(entry["title"])
        if key:  # titles in other alphabets have no key here; never guess about them
            groups[(entry["authorId"], key)].append(entry)
    found = []
    for (_, key), members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda e: (not _has_files(e), e["id"]))
        found.append({"key": key, "author": members[0]["author"], "entries": members, **_classify(members)})
    order = list(KINDS)
    found.sort(key=lambda g: (order.index(g["kind"]), g["author"].lower(), g["key"]))
    return found


def _record(entry: dict, keep: dict, status: str, error: str = "") -> None:
    with local_conn() as conn:
        conn.execute(
            """INSERT INTO cleanup_actions(result_id, scan_id, file_id, book_id, classification, format, author,
                   title, stored_path, local_path, action_kind, status, created_at, completed_at, error, followup)
               VALUES (0, '', 0, ?, 'DUPLICATE', '', ?, ?, '', '', 'HIDE_DUPLICATE', ?, ?, ?, ?, ?)""",
            (entry["id"], entry["author"], entry["title"], status, utc_now(), utc_now(), error or None,
             f"Kept “{keep['title']}” (Bindery book {keep['id']}), which has the files."),
        )
        conn.commit()


def hide_empty(book_id: int, client: BinderyClient | None = None) -> str:
    """Exclude one empty extra entry in Bindery after checking it again."""
    if not settings.allow_actions:
        raise ActionError("Actions are disabled. Turn them on in Settings to hide duplicates.")
    group = next((g for g in find_duplicates([int(book_id)])
                  if any(e["id"] == int(book_id) for e in g["entries"])), None)
    if group is None:
        raise ActionError("Bindery no longer lists this book twice. Nothing was changed.")
    entry = next(e for e in group["entries"] if e["id"] == int(book_id))
    if int(book_id) not in group["hideable"]:
        raise ActionError(f"“{entry['title']}” can't be hidden safely ({group['label'].lower()}). Nothing was changed.")
    keep = next(e for e in group["entries"] if _has_files(e))
    client = client or BinderyClient(api_key=resolve_bindery_api_key(), timeout=30)
    try:
        book = client.toggle_book_excluded(int(book_id))
    except BinderyClientError as exc:
        _record(entry, keep, "failed", str(exc))
        raise ActionError(f"Bindery refused to hide it: {exc}") from exc
    if not (book or {}).get("excluded"):  # the call toggles: it was hidden elsewhere in the meantime
        try:
            client.toggle_book_excluded(int(book_id))
        finally:
            _record(entry, keep, "failed", "Bindery reported it was already hidden; it was left hidden.")
        raise ActionError("Bindery had already hidden this entry. It is still hidden; nothing else changed.")
    _record(entry, keep, "applied")
    return (f"Hid the empty “{entry['title']}” in Bindery and kept “{keep['title']}”. "
            "To undo it, include it again on the book's page in Bindery.")


def hide_all_empty(client: BinderyClient | None = None) -> dict:
    """Hide every safe empty extra entry, each checked again on its own."""
    if not settings.allow_actions:
        raise ActionError("Actions are disabled. Turn them on in Settings to hide duplicates.")
    client = client or BinderyClient(api_key=resolve_bindery_api_key(), timeout=30)
    hidden, problems = 0, []
    for group in find_duplicates():
        for book_id in group["hideable"]:
            try:
                hide_empty(book_id, client)
                hidden += 1
            except ActionError as exc:
                problems.append(str(exc))
    return {"hidden": hidden, "problems": problems}
