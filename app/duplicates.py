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

What BookGuard fixes (with the Fix now button, or every hour only when "Fix
duplicate Bindery entries automatically" is turned on under Settings → Schedule):

* Empty extra entries are hidden with Bindery's own exclude.
* When no entry has files, all but one are hidden (Bindery searched for each).

Nothing with files is changed: an ebook and audiobook split over two entries, and
two copies of the same kind, are only listed.

Only when an entry is proven the kept entry's book: titles and author agree,
Bindery's series data doesn't contradict it, and Bindery's year or language
labels don't disagree, unless a shared ISBN/ASIN or series place proves it anyway.
The labels are often wrong (they come from a random edition), so such pairs are
left for the user with the reason, never decided by the title alone. Every
change is checked again against Bindery right before, recorded in Activity, and
can be undone in Bindery.
"""

from __future__ import annotations

from .library_check import serialized_start, start_allowed

from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
import re
import threading
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
    leading article never matter. Words in brackets do ("Book 1" vs "Book 2"), and so
    does every letter of any alphabet."""
    # Accents go, but letters of every alphabet stay: "战争 1" and "和平 1" must not meet.
    text = "".join(c for c in unicodedata.normalize("NFKD", str(title or "")) if not unicodedata.combining(c))
    text = re.sub(r"['`’]", "", text.casefold()).replace("&", " and ")
    text = re.sub(r"[\W_]+", " ", text).strip()
    return ARTICLE.sub("", text).strip()


LANGUAGES = {"en": "en", "eng": "en", "english": "en", "de": "de", "deu": "de", "ger": "de", "german": "de",
             "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr", "es": "es", "spa": "es", "spanish": "es",
             "nl": "nl", "nld": "nl", "dut": "nl", "dutch": "nl", "sv": "sv", "swe": "sv", "swedish": "sv"}


UNKNOWN_LANGUAGES = {"", "und", "mul", "zxx", "mis", "unknown"}


def _language(value: Any) -> str:
    """One code per language for the page; "und" and blank are unknown."""
    text = str(value or "").strip().lower()
    return "" if text in UNKNOWN_LANGUAGES else LANGUAGES.get(text, text)


def _year(value: Any) -> int | None:
    found = re.match(r"\s*(\d{4})", str(value or ""))
    return int(found.group(1)) if found else None


def _place(position: Any) -> float | None:
    try:
        return float(str(position).strip())
    except ValueError:
        return None


def _identifier(value: Any) -> str:
    text = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()
    return text if len(text) >= 10 else ""


def _tables(conn) -> set[str]:
    return {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _entries(conn, book_ids: list[int] | None = None) -> list[dict]:
    """Every book Bindery shows (not excluded): its files by format and what identifies it."""
    where = "WHERE b.excluded = 0"
    args: list[Any] = []
    if book_ids:
        where += f" AND b.author_id IN (SELECT author_id FROM books WHERE id IN ({','.join('?' * len(book_ids))}))"
        args = [int(i) for i in book_ids]
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
    language = "b.language" if "language" in columns else "''"
    asin = "b.asin" if "asin" in columns else "''"
    books = conn.execute(
        f"""SELECT b.id, b.author_id, b.title, b.release_date, b.ebook_file_path, b.audiobook_file_path,
                   b.file_path, {language} AS language, {asin} AS asin, a.name AS author
            FROM books b JOIN authors a ON a.id = b.author_id {where}""",
        args,
    ).fetchall()
    tables = _tables(conn)
    files: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in conn.execute("SELECT book_id, format, path FROM book_files"):
        files[int(row["book_id"])][str(row["format"])].append(str(row["path"]))
    identifiers: dict[int, set[str]] = defaultdict(set)
    if "editions" in tables:
        edition_columns = {row["name"] for row in conn.execute("PRAGMA table_info(editions)")}
        picked = ", ".join(c if c in edition_columns else f"'' AS {c}" for c in ("isbn_13", "isbn_10", "asin"))
        for row in conn.execute(f"SELECT book_id, {picked} FROM editions"):
            identifiers[int(row["book_id"])].update(filter(None, map(_identifier, (row["isbn_13"], row["isbn_10"], row["asin"]))))
    series: dict[int, list[tuple[int, str]]] = defaultdict(list)
    if "series_books" in tables:
        for row in conn.execute("SELECT book_id, series_id, position_in_series FROM series_books"):
            series[int(row["book_id"])].append((int(row["series_id"]), str(row["position_in_series"] or "")))
    entries = []
    for row in books:
        book_id = int(row["id"])
        found = files.get(book_id, {})
        ebooks = list(found.get("ebook", [])) or [p for p in [row["ebook_file_path"]] if p]
        audio = list(found.get("audiobook", [])) or [p for p in [row["audiobook_file_path"]] if p]
        legacy = [p for p in [row["file_path"]] if p and p not in ebooks + audio]
        entries.append({
            "id": book_id, "authorId": int(row["author_id"]), "author": str(row["author"]),
            "title": str(row["title"]), "year": _year(row["release_date"]),
            "ebooks": ebooks, "audiobooks": audio, "other": legacy,
            "language": _language(row["language"]),
            "ids": identifiers.get(book_id, set()) | ({_identifier(row["asin"])} - {""}),
            "series": series.get(book_id, []),
        })
    return entries


def _has_files(entry: dict) -> bool:
    return bool(entry["ebooks"] or entry["audiobooks"] or entry["other"])


def _conflict(a: dict, b: dict) -> str:
    """Why two entries are not one book, or "": different places in one Bindery series."""
    places_a, places_b = dict(a["series"]), dict(b["series"])
    for series_id in places_a.keys() & places_b.keys():
        first, second = _place(places_a[series_id]), _place(places_b[series_id])
        if first is not None and second is not None and first != second:
            low, high = sorted((first, second))
            return f"they are different numbers ({low:g} and {high:g}) in the same series"
    return ""


def _labels_disagree(entry: dict, keep: dict) -> str:
    """Bindery's year or language labels disagree, or "". Not proof of two books (the
    labels are often wrong), but enough that a matching title alone doesn't decide it."""
    if entry["year"] and keep["year"] and abs(entry["year"] - keep["year"]) > 1:
        return (f"Bindery gives “{entry['title']}” the year {entry['year']} and “{keep['title']}” "
                f"{keep['year']}, and no ISBN or series place shows they are one book")
    if entry["language"] and keep["language"] and entry["language"] != keep["language"]:
        return (f"Bindery labels “{entry['title']}” {entry['language']} and “{keep['title']}” "
                f"{keep['language']}, and no ISBN or series place shows they are one book")
    return ""


def _proof(a: dict, b: dict) -> str:
    """What shows two entries are one book beyond the title, or ""."""
    if a["ids"] & b["ids"]:
        return "the same ISBN or ASIN"
    places_a, places_b = dict(a["series"]), dict(b["series"])
    if any(_place(places_a[s]) is not None and _place(places_a[s]) == _place(places_b[s])
           for s in places_a.keys() & places_b.keys()):
        return "the same place in a series"
    return ""


def _evidence(entries: list[dict], keep: dict) -> tuple[dict[int, str], str]:
    """Each other entry's proof of being the keeper's book, and why anything is left.

    A series contradiction between any two entries leaves the whole group (it may mix
    books). Otherwise each entry is judged against the keeper on its own: a shared
    ISBN/ASIN or series place proves it; a matching title proves it only when Bindery's
    year and language labels don't disagree."""
    for a, b in combinations(entries, 2):
        reason = _conflict(a, b)
        if reason:
            return {}, reason
    proven: dict[int, str] = {}
    left: list[str] = []
    for entry in entries:
        if entry is keep:
            continue
        proof = _proof(entry, keep)
        disagreement = "" if proof else _labels_disagree(entry, keep)
        if disagreement:
            left.append(disagreement)
        else:
            proven[entry["id"]] = proof or "the same title and author"
    return proven, "; ".join(left)


def _keeper(entries: list[dict]) -> dict:
    """The entry to keep: the one with the most formats and files, else the best described."""
    def rank(e: dict) -> tuple:
        formats = bool(e["ebooks"]) + bool(e["audiobooks"])
        files = len(e["ebooks"]) + len(e["audiobooks"]) + len(e["other"])
        return (formats, files, bool(e["series"]), len(e["ids"]), bool(e["year"]), -e["id"])
    return max(entries, key=rank)


def _classify(entries: list[dict]) -> dict:
    with_files = [e for e in entries if _has_files(e)]
    empty = [e for e in entries if not _has_files(e)]
    formats_shared = (sum(1 for e in with_files if e["ebooks"]) > 1
                      or sum(1 for e in with_files if e["audiobooks"]) > 1 or any(e["other"] for e in with_files))
    if not with_files:
        kind = "ALL_EMPTY"
    elif formats_shared:
        kind = "TWO_COPIES"
    elif len(with_files) > 1:
        kind = "SPLIT"
    else:
        kind = "EMPTY_EXTRA"
    keep = _keeper(entries)
    proven, blocked = _evidence(entries, keep)
    hideable = [e["id"] for e in (empty if with_files else entries) if e is not keep and e["id"] in proven]
    proofs = sorted(set(proven.values()))
    proof = " and ".join(proofs) if proofs else ""
    return {"kind": kind, "label": KINDS[kind], "keep": keep["id"], "hideable": hideable,
            "proof": proof, "proven": proven, "blocked": blocked}


def find_duplicates(book_ids: list[int] | None = None) -> list[dict]:
    """Groups of two or more visible books by one author whose titles share a key."""
    with bindery_conn() as conn:
        entries = _entries(conn, book_ids)
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for entry in entries:
        key = title_key(entry["title"])
        if key:
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


def _group_of(book_id: int) -> dict | None:
    return next((g for g in find_duplicates([int(book_id)])
                 if any(e["id"] == int(book_id) for e in g["entries"])), None)


def _record(entry: dict, keep: dict, kind: str, status: str, note: str, error: str = "", auto: bool = False) -> None:
    with local_conn() as conn:
        conn.execute(
            """INSERT INTO cleanup_actions(result_id, scan_id, file_id, book_id, classification, format, author,
                   title, stored_path, local_path, action_kind, status, created_at, completed_at, error, followup)
               VALUES (0, '', 0, ?, ?, '', ?, ?, '', '', ?, ?, ?, ?, ?, ?)""",
            (entry["id"], "DUPLICATE_AUTO" if auto else "DUPLICATE", entry["author"], entry["title"], kind, status,
             utc_now(), utc_now(), error or None, note),
        )
        conn.commit()


def _require_actions() -> None:
    if not settings.allow_actions:
        raise ActionError("Actions are disabled. Turn them on in Settings to fix duplicates.")


def _client(client: BinderyClient | None) -> BinderyClient:
    return client or BinderyClient(api_key=resolve_bindery_api_key(), timeout=30)


def hide_empty(book_id: int, client: BinderyClient | None = None, *, auto: bool = False) -> str:
    """Exclude one empty extra entry in Bindery after checking it again."""
    _require_actions()
    group = _group_of(book_id)
    if group is None:
        raise ActionError("Bindery no longer lists this book twice. Nothing was changed.")
    entry = next(e for e in group["entries"] if e["id"] == int(book_id))
    if int(book_id) not in group["hideable"]:
        why = group["blocked"] or group["label"].lower()
        raise ActionError(f"“{entry['title']}” can't be hidden safely ({why}). Nothing was changed.")
    keep = next(e for e in group["entries"] if e["id"] == group["keep"])
    note = (f"{'Automatically: ' if auto else ''}kept “{keep['title']}” (Bindery book {keep['id']}); "
            f"one book because of {group['proven'][int(book_id)]}.")
    try:
        result = _client(client).exclude_book(int(book_id))
    except BinderyClientError as exc:
        _record(entry, keep, "HIDE_DUPLICATE", "failed", note, str(exc), auto)
        raise ActionError(f"Bindery refused to hide it: {exc}") from exc
    if not result.get("ok"):
        error = str(result.get("error") or "Bindery did not confirm it.")
        _record(entry, keep, "HIDE_DUPLICATE", "failed", note, error, auto)
        raise ActionError(f"Bindery refused to hide it: {error}")
    _record(entry, keep, "HIDE_DUPLICATE", "applied", note, auto=auto)
    return (f"Hid the empty “{entry['title']}” in Bindery and kept “{keep['title']}”. "
            "To undo it, include it again on the book's page in Bindery.")


_LOCK = threading.Lock()
STATUS: dict[str, Any] = {"running": False, "hidden": 0, "problems": [], "finishedAt": ""}


def fix_all(client: BinderyClient | None = None, *, auto: bool = False) -> dict:
    """Hide every proven empty extra entry, each checked again on its own."""
    _require_actions()
    client = _client(client)
    hidden, problems = 0, []
    for group in find_duplicates():
        for book_id in group["hideable"]:
            try:
                hide_empty(book_id, client, auto=auto)
                hidden += 1
            except ActionError as exc:
                problems.append(str(exc))
    return {"hidden": hidden, "problems": problems}


@serialized_start
def start_fix(*, auto: bool = False) -> bool:
    """Run fix_all in the background; False when one is already running."""
    if not start_allowed() or not _LOCK.acquire(blocking=False):
        return False
    STATUS.update(running=True, problems=[])

    def run() -> None:
        try:
            STATUS.update(fix_all(auto=auto))
        except Exception as exc:  # noqa: BLE001 - shown on the page
            STATUS["problems"] = [str(exc)[:500]]
        finally:
            STATUS.update(running=False, finishedAt=datetime.now(timezone.utc).isoformat())
            _LOCK.release()

    threading.Thread(target=run, name="bookguard-duplicates", daemon=True).start()
    return True


def fix_status() -> dict:
    return dict(STATUS)
