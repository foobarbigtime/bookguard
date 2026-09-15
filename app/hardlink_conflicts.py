from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import threading

from .actions import ActionError
from .bindery_client import BinderyClient
from .config import settings
from .db import load_bindery_files, local_conn, utc_now
from .ebook_extraction import extract_ebook_identity
from .ebook_security import inspect_ebook_security
from .file_safety import is_within, sha256_file
from .verification_engine import classify_identity, _title_identity_match


_lock = threading.Lock()
_POLICY = (
    "verification_enabled", "verification_file_signatures",
    "verification_archive_safety", "verification_epub_structure",
    "verification_pdf_integrity",
)
_BOOK_FIELDS = ("id", "title", "ebookFilePath", "audiobookFilePath", "mediaType")


def _local_path(row: dict) -> Path:
    prefix = Path(settings.ebook_bindery_prefix)
    stored = Path(row["stored_path"])
    try:
        relative = stored.relative_to(prefix)
    except ValueError as exc:
        raise ActionError("The tracked path is outside the configured ebook prefix.") from exc
    root = Path(settings.ebook_root).resolve(strict=True)
    path = root / relative
    if ".." in relative.parts:
        raise ActionError("Parent traversal is not allowed.")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ActionError("Symbolic links are not eligible for hard-link correction.")
    if not is_within(path.resolve(strict=True), root):
        raise ActionError("The resolved file is outside the ebook root.")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ActionError("The tracked path must be a regular file.")
    return path


def _fingerprint(path: Path) -> dict:
    info = path.stat()
    return {
        "device": info.st_dev, "inode": info.st_ino, "links": info.st_nlink,
        "bytes": info.st_size, "mtimeNs": info.st_mtime_ns, "ctimeNs": info.st_ctime_ns,
    }


def _book_snapshot(book: dict) -> dict:
    snapshot = {key: book.get(key) for key in _BOOK_FIELDS}
    snapshot["files"] = sorted([
        {key: entry.get(key) for key in ("id", "bookId", "format", "path")}
        for entry in book.get("bookFiles") or []
    ], key=lambda entry: (str(entry["id"]), str(entry["path"])))
    return snapshot


def _check_book(row: dict, book: dict, rows: list[dict]) -> dict:
    snapshot = _book_snapshot(book)
    expected = sorted(
        (r["file_id"], r["format"], r["stored_path"])
        for r in rows if r["book_id"] == row["book_id"]
    )
    actual = sorted((f["id"], f["format"], f["path"]) for f in snapshot["files"])
    author = book.get("author") or {}
    api_author = book.get("authorName") or (author.get("name") if isinstance(author, dict) else "")
    if (book.get("id") != row["book_id"] or book.get("title") != row["title"]
            or api_author != row["author"] or actual != expected
            or book.get("ebookFilePath") != row["stored_path"]):
        raise ActionError("Bindery's API and database do not agree on the current identity and files.")
    return snapshot


def _require_idle(client: BinderyClient) -> None:
    if str(client.get_setting("autoGrab.enabled")).lower() != "false":
        raise ActionError("Bindery auto-grab must be disabled before correction.")
    if str(client.get_setting("import.mode")).lower() != "external":
        raise ActionError("Bindery must be in external import mode before correction.")
    queue = client.list_queue()
    if not isinstance(queue, dict) or queue.get("partial") is not False:
        raise ActionError("A complete Bindery queue snapshot is required.")
    items = queue.get("items")
    if not isinstance(items, list):
        raise ActionError("Bindery returned an invalid queue snapshot.")
    historical = {"cancelled", "failed", "importblocked", "imported", "removed"}
    if any(not isinstance(item, dict) or str(item.get("status") or "").lower()
           not in historical for item in items):
        raise ActionError("An active Bindery queue record blocks correction.")


def _snapshot(file_id: int, client: BinderyClient) -> dict:
    if not all(getattr(settings, key) for key in _POLICY):
        raise ActionError("All core ebook verification checks must be enabled.")
    rows = load_bindery_files()
    wrong = next((r for r in rows if r["file_id"] == file_id), None)
    if not wrong or wrong["format"] != "ebook":
        raise ActionError("The selected ebook association no longer exists.")
    source = _local_path(wrong)
    if source.suffix.lower() != ".epub" or not source.name.startswith(".bindery-stage-"):
        raise ActionError("This correction is limited to Bindery staging EPUB aliases.")
    info = _fingerprint(source)
    linked = []
    for row in rows:
        if row["format"] != "ebook":
            continue
        try:
            path = _local_path(row)
            other = _fingerprint(path)
        except (ActionError, OSError):
            continue
        if (other["device"], other["inode"]) == (info["device"], info["inode"]):
            linked.append((row, path))
    if len(linked) != 2 or info["links"] != 2:
        raise ActionError("Exactly two tracked paths and two physical hard links are required.")
    retained, destination = next(pair for pair in linked if pair[0]["file_id"] != file_id)
    if (retained["book_id"] == wrong["book_id"] or destination.parent != source.parent
            or destination.name.startswith(".bindery-stage-")
            or destination.suffix.lower() != ".epub"):
        raise ActionError("A distinct book must own the final EPUB in the same folder.")
    if _title_identity_match(wrong["title"], retained["title"]):
        raise ActionError("Equivalent catalog titles do not prove a wrong association.")

    digest = sha256_file(source)
    safety = inspect_ebook_security(source)
    if not safety["safe"]:
        raise ActionError("The shared EPUB failed deterministic safety validation.")
    extracted = extract_ebook_identity(str(destination))
    verdict, confidence, evidence = classify_identity(
        retained, extracted.metadata, extracted.text, extracted.identifiers,
        list(extracted.notes), extracted.front_text,
    )
    _, _, conflicting = classify_identity(
        wrong, extracted.metadata, extracted.text, extracted.identifiers,
        list(extracted.notes), extracted.front_text,
    )
    position = evidence["content"]["expected_signal"]["title_first_position"]
    wrong_position = conflicting["content"]["expected_signal"]["title_first_position"]
    if (verdict != "VERIFIED_CORRECT" or confidence < 99 or position is None
            or position > 500 or (wrong_position is not None and wrong_position <= 500)
            or extracted.source != "native-epub"):
        raise ActionError("Native title-page evidence does not uniquely verify the retained book.")

    books = {
        str(row["book_id"]): _check_book(row, client.get_book(row["book_id"]), rows)
        for row in (wrong, retained)
    }
    _require_idle(client)
    # Detect filesystem changes during hashing, extraction, or remote reads.
    if (_fingerprint(source) != info or _fingerprint(destination) != info
            or sha256_file(destination) != digest or load_bindery_files() != rows):
        raise ActionError("The filesystem or registration changed during verification.")
    return {
        "wrong": wrong, "retained": retained, "books": books,
        "source": str(source), "destination": str(destination),
        "fingerprint": info, "sha256": digest, "verification": evidence,
    }


def _token(snapshot: dict) -> str:
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _init_journal() -> None:
    with local_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS hardlink_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT
        )""")
        conn.commit()


def correction_history() -> list[dict]:
    _init_journal()
    with local_conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id, file_id, status, created_at, completed_at, error "
            "FROM hardlink_corrections ORDER BY id DESC LIMIT 100"
        )]


def _require_no_unresolved() -> None:
    _init_journal()
    with local_conn() as conn:
        unresolved = conn.execute(
            "SELECT id FROM hardlink_corrections WHERE status NOT IN ('applied', 'cancelled') LIMIT 1"
        ).fetchone()
    if unresolved:
        raise ActionError("An interrupted or uncertain correction needs review before another action.")


def conflict_preview(file_id: int, client: BinderyClient | None = None) -> dict:
    try:
        snapshot = _snapshot(file_id, client or BinderyClient())
        _require_no_unresolved()
        return {
            "safe": True, "ready": bool(settings.allow_actions),
            "fileId": file_id, "token": _token(snapshot), **snapshot,
            "reason": ("The staging association conflicts with the verified final book."
                       if settings.allow_actions else "Evidence passed; Bindery actions are disabled."),
        }
    except Exception as exc:
        return {"safe": False, "ready": False, "fileId": file_id, "reason": str(exc)}


def hardlink_conflicts() -> dict:
    groups: dict[tuple[int, int], list[dict]] = {}
    for row in load_bindery_files():
        if row["format"] != "ebook":
            continue
        try:
            path = _local_path(row)
            info = path.stat()
        except (ActionError, OSError):
            continue
        if info.st_nlink > 1:
            groups.setdefault((info.st_dev, info.st_ino), []).append(row)
    conflicts = []
    for rows in groups.values():
        if len({row["book_id"] for row in rows}) < 2:
            continue
        staged = [row for row in rows if Path(row["stored_path"]).name.startswith(".bindery-stage-")]
        conflicts.append({
            "associations": rows,
            "candidateFileId": staged[0]["file_id"] if len(staged) == 1 else None,
        })
    return {"items": conflicts, "actionsEnabled": bool(settings.allow_actions),
            "history": correction_history()}


def _verify_postconditions(snapshot: dict, client: BinderyClient) -> None:
    wrong, retained = snapshot["wrong"], snapshot["retained"]
    rows = load_bindery_files()
    if any(r["file_id"] == wrong["file_id"] or r["stored_path"] == wrong["stored_path"] for r in rows):
        raise ActionError("The wrong association is still registered after the request.")
    if not any(all(r.get(k) == retained.get(k) for k in
                   ("file_id", "book_id", "format", "stored_path", "title", "author"))
               for r in rows):
        raise ActionError("The retained ebook registration changed; review is required.")
    after_books = {str(row["book_id"]): _book_snapshot(client.get_book(row["book_id"]))
                   for row in (wrong, retained)}
    expected_wrong = snapshot["books"][str(wrong["book_id"])]
    after_wrong = after_books[str(wrong["book_id"])]
    expected_files = [f for f in expected_wrong["files"] if f["id"] != wrong["file_id"]]
    if (after_books[str(retained["book_id"])] != snapshot["books"][str(retained["book_id"])]
            or after_wrong["files"] != expected_files
            or after_wrong["audiobookFilePath"] != expected_wrong["audiobookFilePath"]
            or after_wrong["title"] != expected_wrong["title"]
            or after_wrong["ebookFilePath"]):
        raise ActionError("Book or audiobook postconditions failed; review is required.")
    expected_db = {
        (f["id"], int(book_id), f["format"], f["path"])
        for book_id, book in snapshot["books"].items()
        for f in book["files"] if f["id"] != wrong["file_id"]
    }
    actual_db = {
        (r["file_id"], r["book_id"], r["format"], r["stored_path"])
        for r in rows if str(r["book_id"]) in snapshot["books"]
    }
    if actual_db != expected_db:
        raise ActionError("Database postconditions failed; review is required.")
    for row, value in ((wrong, snapshot["source"]), (retained, snapshot["destination"])):
        path = _local_path(row)
        if (str(path) != value or _fingerprint(path) != snapshot["fingerprint"]
                or sha256_file(path) != snapshot["sha256"]):
            raise ActionError("Filesystem postconditions failed; review is required.")


def reconcile_correction(correction_id: int, client: BinderyClient | None = None) -> dict:
    """Explicitly resolve a journal entry by observing state, never repeating the API mutation."""
    if not _lock.acquire(blocking=False):
        raise ActionError("Another hard-link correction is running.")
    try:
        _init_journal()
        with local_conn() as conn:
            row = conn.execute("SELECT * FROM hardlink_corrections WHERE id=?",
                               (correction_id,)).fetchone()
        if not row:
            raise ActionError("Correction record not found.")
        if row["status"] in {"applied", "cancelled"}:
            return {"ok": True, "status": row["status"], "message": "Correction already reconciled."}
        snapshot = json.loads(row["snapshot_json"])
        client = client or BinderyClient()
        _require_idle(client)
        if any(r["file_id"] == row["file_id"] for r in load_bindery_files()):
            if _token(_snapshot(row["file_id"], client)) != _token(snapshot):
                raise ActionError("The original proof changed; the interrupted correction still needs review.")
            status = "cancelled"
            message = "No registration change occurred. The interrupted correction was cancelled; inspect a fresh preview to retry."
        else:
            _verify_postconditions(snapshot, client)
            status = "applied"
            message = "The association was removed and all protected files and audiobook registrations are intact."
        with local_conn() as conn:
            conn.execute("UPDATE hardlink_corrections SET status=?, completed_at=?, error=NULL WHERE id=?",
                         (status, utc_now(), correction_id))
            conn.commit()
        return {"ok": True, "status": status, "message": message, "filesChanged": False}
    except Exception as exc:
        if isinstance(exc, ActionError):
            raise
        raise ActionError(str(exc)) from exc
    finally:
        _lock.release()


def correct_hardlink_conflict(file_id: int, token: str,
                              client: BinderyClient | None = None) -> dict:
    if not settings.allow_actions:
        raise ActionError("Bindery actions are disabled; correction is blocked.")
    if not _lock.acquire(blocking=False):
        raise ActionError("Another hard-link correction is running.")
    journal_id = None
    try:
        _require_no_unresolved()
        client = client or BinderyClient()
        snapshot = _snapshot(file_id, client)
        if not token or _token(snapshot) != token:
            raise ActionError("The preview changed. Inspect a fresh preview before correction.")
        with local_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO hardlink_corrections(file_id, snapshot_json, status, created_at) "
                "VALUES (?, ?, 'running', ?)",
                (file_id, json.dumps(snapshot, sort_keys=True), utc_now()),
            )
            journal_id = int(cursor.lastrowid)
            conn.commit()
        # Recheck the complete proof after persisting recovery state, before mutation.
        if (not settings.allow_actions or _token(_snapshot(file_id, client)) != token):
            raise ActionError("Safety state changed before correction.")
        wrong = snapshot["wrong"]
        client.deregister_file(wrong["book_id"], wrong["stored_path"])
        _verify_postconditions(snapshot, client)
        with local_conn() as conn:
            conn.execute("UPDATE hardlink_corrections SET status='applied', completed_at=? WHERE id=?",
                         (utc_now(), journal_id))
            conn.commit()
        return {"ok": True, "correctionId": journal_id, "filesChanged": False,
                "message": "Wrong ebook association removed. Both files and audiobook registrations were preserved."}
    except Exception as exc:
        if journal_id is not None:
            with local_conn() as conn:
                conn.execute(
                    "UPDATE hardlink_corrections SET status='needs_review', completed_at=?, error=? WHERE id=?",
                    (utc_now(), str(exc)[:1000], journal_id),
                )
                conn.commit()
        if isinstance(exc, ActionError):
            raise
        raise ActionError(str(exc)) from exc
    finally:
        _lock.release()
