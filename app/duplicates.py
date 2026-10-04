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

What BookGuard fixes, automatically after each check when "Fix duplicates
automatically" is on (and with the Fix now button):

* Empty extra entries are hidden with Bindery's own exclude.
* When no entry has files, all but one are hidden (Bindery searched for each).
* An ebook on one entry and the audiobook on another are joined with Bindery's
  Fix match onto one entry; the emptied entry is then hidden.

Only when the entries are one book: titles and author agree and Bindery's series
data doesn't contradict it (different places in one series). Bindery's language
and year labels are not used: on real libraries they come from a random edition
("The Racketeer" labelled Swedish, "Nightingale" 2025) and only caused false
alarms. The files themselves are compared by the file clean-up, which handles
two copies of the same kind. Every change is checked again against Bindery right
before, recorded in Activity, and can be undone in Bindery.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path, PurePosixPath
import re
import threading
import time
from typing import Any
import unicodedata

from .actions import ActionError, resolve_bindery_api_key
from .bindery_client import BinderyClient, BinderyClientError
from .config import settings
from .db import bindery_conn, local_conn, utc_now
from .file_safety import is_within

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
MERGE_WAIT_SECONDS = 600


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
    """Why two entries are not one book, or "". Only Bindery's series places count:
    its language and year labels are too often wrong to stand against a matching title."""
    places_a, places_b = dict(a["series"]), dict(b["series"])
    for series_id in places_a.keys() & places_b.keys():
        first, second = _place(places_a[series_id]), _place(places_b[series_id])
        if first is not None and second is not None and first != second:
            low, high = sorted((first, second))
            return f"they are different numbers ({low:g} and {high:g}) in the same series"
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
    """Each other entry's proof of being the keeper's book, and why the group is left.

    Any contradiction between two entries leaves the whole group (it may mix books)."""
    for a, b in combinations(entries, 2):
        reason = _conflict(a, b)
        if reason:
            return {}, reason
    proven = {e["id"]: _proof(e, keep) or "the same title and author" for e in entries if e is not keep}
    return proven, ""


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
    moves: list[dict] = []
    if kind == "SPLIT" and all(e["id"] in proven for e in with_files if e is not keep):
        moves = [{"from": e["id"], "format": fmt, "path": path}
                 for e in with_files if e is not keep
                 for fmt, paths in (("ebook", e["ebooks"]), ("audiobook", e["audiobooks"])) for path in paths]
    proofs = sorted(set(proven.values()))
    proof = " and ".join(proofs) if proofs else ""
    return {"kind": kind, "label": KINDS[kind], "keep": keep["id"], "hideable": hideable, "moves": moves,
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


def _inside_library(destination: str, file_format: str) -> bool:
    prefix = settings.audiobook_bindery_prefix if file_format == "audiobook" else settings.ebook_bindery_prefix
    path = PurePosixPath(destination)
    return ".." not in path.parts and is_within(Path(destination), Path(prefix)) and path != PurePosixPath(prefix)


def _formats_of(book_id: int) -> dict[str, list[str]]:
    with bindery_conn() as conn:
        rows = conn.execute("SELECT format, path FROM book_files WHERE book_id=?", (int(book_id),)).fetchall()
    found: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        found[str(row["format"])].append(str(row["path"]))
    return found


def merge_split(keep_id: int, client: BinderyClient | None = None, *, auto: bool = False,
                wait_seconds: int = MERGE_WAIT_SECONDS, poll_seconds: float = 5) -> str:
    """Join an ebook and an audiobook split over two entries onto one, with Bindery's Fix match."""
    _require_actions()
    group = _group_of(keep_id)
    if group is None or group["keep"] != int(keep_id) or not group["moves"]:
        raise ActionError("These entries are no longer an ebook/audiobook split. Nothing was changed.")
    client = _client(client)
    keep = next(e for e in group["entries"] if e["id"] == int(keep_id))
    # Every move must be one Bindery's Fix match can do inside the library, before any starts.
    for move in group["moves"]:
        preview = client.preview_manual_reassignment(move["path"], int(keep_id), file_format=move["format"])
        if preview.get("status") not in {"move", "noop"} or not _inside_library(
                str(preview.get("destination") or ""), move["format"]):
            raise ActionError(f"Bindery's Fix match can't move “{move['path']}” onto “{keep['title']}” "
                              f"({preview.get('message') or preview.get('status') or 'no answer'}). Nothing was changed.")
    for move in group["moves"]:
        source = next(e for e in group["entries"] if e["id"] == move["from"])
        note = (f"{'Automatically: ' if auto else ''}moved the {move['format']} of “{source['title']}” onto "
                f"“{keep['title']}” (Bindery book {keep['id']}); one book because of {group['proven'][move['from']]}.")
        try:
            client.reassign_manual_import(move["path"], int(keep_id), file_format=move["format"])
        except BinderyClientError as exc:
            _record(source, keep, "MERGE_DUPLICATE", "failed", note, str(exc), auto)
            raise ActionError(f"Bindery refused to move it: {exc}") from exc
        deadline = time.monotonic() + wait_seconds
        while not _formats_of(int(keep_id)).get(move["format"]):
            if time.monotonic() > deadline:
                _record(source, keep, "MERGE_DUPLICATE", "attention", note,
                        "Bindery did not finish moving it in time; check the book in Bindery.", auto)
                raise ActionError("Bindery did not finish moving it in time.")
            time.sleep(poll_seconds)
        _record(source, keep, "MERGE_DUPLICATE", "applied", note, auto=auto)
    # The moved-from entries are empty now: hide them the usual way (checked again).
    for book_id in {m["from"] for m in group["moves"]}:
        hide_empty(book_id, client, auto=auto)
    return f"Joined the ebook and audiobook onto “{keep['title']}” and hid the emptied entry."


_LOCK = threading.Lock()
STATUS: dict[str, Any] = {"running": False, "hidden": 0, "merged": 0, "problems": [], "finishedAt": ""}


def fix_all(client: BinderyClient | None = None, *, auto: bool = False, **merge_options) -> dict:
    """Every safe fix: hide proven empty extras, then join proven ebook/audiobook splits."""
    _require_actions()
    client = _client(client)
    hidden, merged, problems = 0, 0, []
    for group in find_duplicates():
        for book_id in group["hideable"]:
            try:
                hide_empty(book_id, client, auto=auto)
                hidden += 1
            except ActionError as exc:
                problems.append(str(exc))
    for group in find_duplicates():
        if group["moves"]:
            try:
                merge_split(group["keep"], client, auto=auto, **merge_options)
                merged += 1
            except (ActionError, BinderyClientError) as exc:
                problems.append(str(exc))
    return {"hidden": hidden, "merged": merged, "problems": problems}


def start_fix(*, auto: bool = False) -> bool:
    """Run fix_all in the background; False when one is already running."""
    if not _LOCK.acquire(blocking=False):
        return False

    def run() -> None:
        STATUS.update(running=True, problems=[])
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
