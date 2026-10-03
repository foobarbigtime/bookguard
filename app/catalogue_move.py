"""Move a misfiled ebook to the Bindery book it really is.

When verification proves a file is another catalogued book's *missing ebook*
(ISBN and the file's own title agree, and that book has no ebook file), the
fix is not a download: Bindery's exact-path reassign detaches the file from the
wrong book and imports it for the right one, moving it into that book's folder.

BookGuard never touches the file itself; Bindery performs the move. BookGuard:

1. re-proves the relationship at the moment of acting (a forced verification,
   including the malware scan);
2. checks that the Bindery actions switch is on, Bindery is not in external
   import mode (which skips file operations), the target still has no ebook,
   the source path is tracked by exactly the expected book, and Bindery's
   read-only preview reports a ``move`` to a destination inside the library;
3. records the plan durably before asking Bindery;
4. confirms afterwards that the target tracks the new path, the source no
   longer tracks the old one, and the file at the destination has the verified
   fingerprint. Bindery moves asynchronously, so an unconfirmed move is left
   ``pending`` and can be reconciled later without repeating the request.

Operator-confirmed only; never automatic.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable

from .bindery_client import BinderyClient, BinderyClientError
from .catalogue_relationship import library_path
from .config import settings
from .db import bindery_conn, local_conn
from .file_safety import sha256_file
from .language_detection import declared_language

CONFIRMATION = "MOVE_TO_CORRECT_BOOK"
CONFIRM_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 3


class CatalogueMoveError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_moves_db() -> None:
    with local_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalogue_moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id INTEGER NOT NULL,
                source_book_id INTEGER NOT NULL,
                target_book_id INTEGER NOT NULL,
                target_title TEXT NOT NULL,
                source_path TEXT NOT NULL,
                destination TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_catalogue_moves_result ON catalogue_moves(result_id);
            """
        )
        conn.commit()


def _ebook_paths(book_id: int) -> list[str]:
    with bindery_conn() as conn:
        rows = conn.execute(
            "SELECT path FROM book_files WHERE book_id = ? AND format = 'ebook' ORDER BY id",
            (int(book_id),),
        ).fetchall()
    return [str(row["path"]) for row in rows]


def _owners_of_path(path: str) -> list[int]:
    with bindery_conn() as conn:
        rows = conn.execute(
            "SELECT book_id FROM book_files WHERE path = ? AND format = 'ebook'", (path,)
        ).fetchall()
    return [int(row["book_id"]) for row in rows]


def _verified_sha256(verification: dict) -> str:
    snapshot = ((verification.get("evidence") or {}).get("security") or {}).get("sourceSnapshot") or {}
    value = str(snapshot.get("sha256") or "")
    return value if value.startswith("sha256:") else (f"sha256:{value}" if value else "")


def move_preview(
    result: dict,
    verification: dict,
    client: BinderyClient,
) -> dict[str, Any]:
    """Every check for one move, read-only. Eligible only with no blockers."""
    blockers: list[str] = []
    evidence = verification.get("evidence") or {}
    catalogue = evidence.get("catalogue") or {}
    source_path = str(result.get("stored_path") or "")
    source_book = int(result.get("book_id") or 0)
    target_book = int(catalogue.get("bookId") or 0)
    preview: dict[str, Any] = {
        "resultId": result.get("id"),
        "sourceBookId": source_book,
        "sourcePath": source_path,
        "targetBookId": target_book,
        "targetTitle": str(catalogue.get("title") or ""),
        "sha256": _verified_sha256(verification),
        "destination": "",
        # Not a blocker: a translation can be exactly the book it says it is.
        # Shown so the operator can decide whether they want it at all.
        "language": declared_language(result),
    }

    if result.get("format") != "ebook":
        blockers.append("notAnEbook")
    if verification.get("verdict") != "WRONG_CONTENT" or catalogue.get("relationship") != "missing_ebook":
        blockers.append("notProvenMissingEbookOfAnotherBook")
    if not preview["sha256"]:
        blockers.append("noVerifiedFingerprint")
    if not settings.allow_actions:
        blockers.append("binderyActionsDisabled")

    if not blockers:
        try:
            mode = str(client.get_setting("import.mode") or "auto").casefold()
        except BinderyClientError:
            mode = "unknown"
        if mode in {"external", "unknown"}:
            blockers.append("binderyImportModeNotNormal")
        if _ebook_paths(target_book):
            blockers.append("targetAlreadyHasEbook")
        if _owners_of_path(source_path) != [source_book]:
            blockers.append("sourceNotTrackedByThisBookOnly")
        try:
            answer = client.preview_manual_reassignment(source_path, target_book, file_format="ebook")
        except BinderyClientError as exc:
            answer = {"status": "error", "message": str(exc)}
        preview["binderyPreview"] = {"status": answer.get("status"), "message": answer.get("message")}
        destination = str(answer.get("destination") or "")
        preview["destination"] = destination
        if answer.get("status") != "move":
            blockers.append(f"binderyPreview:{answer.get('status')}")
        elif not library_path_parent_ok(destination):
            blockers.append("destinationOutsideLibrary")

    preview["blockers"] = blockers
    preview["eligible"] = not blockers
    return preview


def library_path_parent_ok(destination: str) -> bool:
    """The destination must lie under Bindery's ebook library prefix."""
    try:
        Path(destination).relative_to(Path(settings.ebook_bindery_prefix))
    except ValueError:
        return False
    return ".." not in Path(destination).parts


def _observe(move: dict) -> dict[str, Any]:
    """Read-only: has Bindery completed exactly the planned move?"""
    target_paths = _ebook_paths(move["target_book_id"])
    source_still_tracked = move["source_book_id"] in _owners_of_path(move["source_path"])
    tracked = move["destination"] in target_paths
    fingerprint = ""
    local = library_path(move["destination"]) if tracked else None
    if local is not None:
        try:
            fingerprint = f"sha256:{sha256_file(local)}"
        except OSError:
            fingerprint = ""
    old_copy_present = library_path(move["source_path"]) is not None
    return {
        "targetTracksDestination": tracked,
        "sourceStillTracked": source_still_tracked,
        "fingerprintMatches": bool(fingerprint) and fingerprint == move["sha256"],
        "fingerprintRead": bool(fingerprint),
        "oldCopyStillOnDisk": old_copy_present,
    }


def _status_for(observed: dict) -> str:
    if observed["targetTracksDestination"] and not observed["sourceStillTracked"]:
        if observed["fingerprintMatches"]:
            return "confirmed"
        if observed["fingerprintRead"]:
            return "fingerprint_mismatch"
    return "pending"


def _save(move_id: int, status: str, detail: dict) -> None:
    with local_conn() as conn:
        conn.execute(
            "UPDATE catalogue_moves SET status=?, detail_json=?, updated_at=? WHERE id=?",
            (status, json.dumps(detail), _utc_now(), move_id),
        )
        conn.commit()


def get_move(move_id: int) -> dict | None:
    init_moves_db()
    with local_conn() as conn:
        row = conn.execute("SELECT * FROM catalogue_moves WHERE id=?", (int(move_id),)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["detail"] = json.loads(item.pop("detail_json") or "{}")
    return item


def list_moves(limit: int = 100) -> list[dict]:
    init_moves_db()
    with local_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM catalogue_moves ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [get_move(int(row["id"])) for row in rows]


def move_to_correct_book(
    result: dict,
    confirm: str,
    *,
    verify: Callable[..., dict],
    client: BinderyClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = CONFIRM_TIMEOUT_SECONDS,
) -> dict:
    """Re-prove, record, ask Bindery to move once, then confirm the result."""
    if confirm != CONFIRMATION:
        raise CatalogueMoveError(f"Explicit {CONFIRMATION} confirmation is required.")
    client = client or BinderyClient()
    fresh = verify(result, force=True)
    preview = move_preview(result, fresh, client)
    if not preview["eligible"]:
        raise CatalogueMoveError(
            "The move is not safe right now: " + ", ".join(preview["blockers"]) + "."
        )

    init_moves_db()
    now = _utc_now()
    with local_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO catalogue_moves(result_id, source_book_id, target_book_id, target_title,
                source_path, destination, sha256, status, detail_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?)
            """,
            (
                int(result["id"]), preview["sourceBookId"], preview["targetBookId"],
                preview["targetTitle"], preview["sourcePath"], preview["destination"],
                preview["sha256"], json.dumps({"preview": preview}), now, now,
            ),
        )
        move_id = int(cursor.lastrowid)
        conn.commit()

    try:
        client.reassign_manual_import(preview["sourcePath"], preview["targetBookId"], file_format="ebook")
    except BinderyClientError as exc:
        _save(move_id, "request_failed", {"preview": preview, "error": str(exc)[:500]})
        raise CatalogueMoveError(f"Bindery refused the move: {exc}") from exc

    return reconcile_move(move_id, sleep=sleep, timeout_seconds=timeout_seconds)


def reconcile_move(
    move_id: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = 0,
) -> dict:
    """Check, read-only, whether Bindery completed the planned move. Never re-requests it."""
    move = get_move(move_id)
    if move is None:
        raise CatalogueMoveError("Unknown move.")
    if move["status"] in {"confirmed", "request_failed"}:
        return move
    waited = 0.0
    while True:
        observed = _observe(move)
        status = _status_for(observed)
        if status != "pending" or waited >= timeout_seconds:
            break
        sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
    _save(move_id, status, {**move["detail"], "observed": observed})
    return get_move(move_id)
