"""Put back: undo one Triage quarantine by moving the file to its original path.

A quarantine moved the file out of the library and detached it from Bindery.
Put back moves the exact same bytes back (never replacing anything), then gets
Bindery to track the file on the same book again (a library scan, or adopting
the unmatched file to that book) and turns monitoring back on if the
quarantine turned it off. It is recorded as a PUT_BACK cleanup row, so it
shows in Activity like the quarantine did.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import threading
import time

from .actions import ActionError, resolve_bindery_api_key
from .bindery_client import BinderyClient, BinderyClientError
from .config import ConfigurationError, load_automation_settings, settings
from .db import (
    associations_inside_path,
    bindery_files_for_book,
    cleanup_action_by_id,
    create_cleanup_action,
    finish_cleanup_action,
    local_conn,
    record_cleanup_location,
    set_cleanup_followup,
)
from .file_safety import is_within, sha256_file
from .quarantine_fs import QuarantineMoveError, move_out_of_quarantine

_LOCK = threading.Lock()


def _library_roots() -> list[Path]:
    roots = [settings.audiobook_root, settings.ebook_root]
    try:
        roots.append(load_automation_settings().ebook_action_root)
    except ConfigurationError:
        pass
    return [Path(root).resolve() for root in roots if root]


def _already_put_back(cleanup_id: int) -> bool:
    with local_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM cleanup_actions WHERE put_back_of=? AND status IN ('applied', 'running', 'attention') LIMIT 1",
            (int(cleanup_id),),
        ).fetchone()
    return row is not None


def put_back_preview(cleanup_id: int, *, check_bytes: bool = True) -> dict | None:
    """Say whether one quarantine can be put back. None when it is not a quarantine."""
    row = cleanup_action_by_id(cleanup_id)
    if not row or "QUARANTINE" not in str(row.get("action_kind") or "").upper():
        return None

    def refuse(reason: str) -> dict:
        return {"safe": False, "reason": reason, "row": row}

    if row.get("status") != "applied":
        return refuse("This quarantine did not finish, so there is nothing to put back.")
    if _already_put_back(cleanup_id):
        return refuse("This book was already put back.")
    original_text, quarantined_text = row.get("original_path"), row.get("quarantine_path")
    expected_sha256, expected_size = row.get("sha256"), row.get("size_bytes")
    if not original_text or not quarantined_text or not expected_sha256 or expected_size is None:
        return refuse("This quarantine was made before BookGuard recorded enough to put it back.")

    quarantined, original = Path(quarantined_text), Path(original_text)
    if not is_within(quarantined.resolve(), Path(settings.quarantine_root).resolve()):
        return refuse("The quarantined file is not inside the quarantine folder.")
    destination = original.parent.resolve() / original.name
    if not any(is_within(destination, root) and destination != root for root in _library_roots()):
        return refuse("The original path is outside the library folders.")
    try:
        replacement = bindery_files_for_book(int(row["book_id"]), str(row["format"]))
    except Exception:
        return refuse("BookGuard can't read Bindery's database right now.")
    if replacement:
        return refuse("Bindery already has another copy of this book. Remove it in Bindery first.")
    if not original.parent.is_dir():
        return refuse("The original folder no longer exists.")
    if os.path.lexists(original):
        return refuse("Something is already at the original path. Nothing was moved.")
    try:
        info = os.lstat(quarantined)
    except FileNotFoundError:
        return refuse("The quarantined file is missing.")
    if not stat.S_ISREG(info.st_mode):
        return refuse("The quarantined path is not a regular file.")
    if info.st_size != int(expected_size):
        return refuse("The quarantined file has changed size since it was quarantined.")
    if check_bytes and sha256_file(quarantined) != expected_sha256:
        return refuse("The quarantined file has changed since it was quarantined.")
    return {"safe": True, "reason": "", "row": row}


def put_back(cleanup_id: int) -> tuple[int, str]:
    """Move one quarantined file back. Returns the PUT_BACK row id and a message."""
    if not settings.allow_actions:
        raise ActionError("Actions are disabled. Turn them on in Settings to put a book back.")
    with _LOCK:
        preview = put_back_preview(cleanup_id)
        if preview is None:
            raise ActionError("That record is not a quarantine.")
        if not preview["safe"]:
            raise ActionError(preview["reason"])
        row = preview["row"]
        record = {**row, "id": row["result_id"]}
        put_back_id = create_cleanup_action(record, "PUT_BACK")
        record_cleanup_location(
            put_back_id,
            original_path=row["original_path"],
            quarantine_path=row["quarantine_path"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            put_back_of=int(cleanup_id),
        )
        try:
            move_out_of_quarantine(
                Path(row["quarantine_path"]),
                Path(row["original_path"]),
                expected_sha256=row["sha256"],
            )
        except QuarantineMoveError as exc:
            finish_cleanup_action(put_back_id, "failed", str(exc)[:1000])
            raise ActionError(f"Put back failed: {exc}") from exc
        finish_cleanup_action(put_back_id, "applied")

    _start_followup(put_back_id, row)
    return put_back_id, (
        "Put back. BookGuard is asking Bindery to find the file again; "
        "Activity shows the result in a few minutes."
    )


# ---- after the move: get Bindery to track the file again ------------------

FOLLOWUP_TIMEOUT_SECONDS = 30 * 60
FOLLOWUP_POLL_SECONDS = 10


def _start_followup(put_back_id: int, row: dict) -> None:
    threading.Thread(
        target=relink_in_bindery, args=(put_back_id, row), name="bookguard-put-back", daemon=True
    ).start()


def _tracked(row: dict) -> bool:
    return any(
        int(item["book_id"]) == int(row["book_id"])
        for item in associations_inside_path(row["stored_path"])
    )


def _unmatched_row(listing: dict, stored_path: str) -> dict | None:
    """The unmatched row holding exactly this file, by Bindery's own paths."""
    stored = PurePosixPath(stored_path)
    for item in listing.get("items") or []:
        found = PurePosixPath(str(item.get("rootPath") or "")) / str(item.get("relPath") or "")
        if found == stored or (found == stored.parent and stored.name in (item.get("members") or [])):
            return item
    return None


def relink_in_bindery(put_back_id: int, row: dict) -> str:
    """Scan, adopt the file to its own book if the scan did not, then monitor again."""
    book_id = int(row["book_id"])
    try:
        client = BinderyClient(api_key=resolve_bindery_api_key(), timeout=30)
        stem = PurePosixPath(row["stored_path"]).stem
        before = (client.list_unmatched(search=stem).get("scan") or {}).get("ranAt")
        client.scan_library()
        deadline = time.monotonic() + FOLLOWUP_TIMEOUT_SECONDS
        while not _tracked(row):
            listing = client.list_unmatched(search=stem, file_format=row.get("format"))
            scan = listing.get("scan") or {}
            if not scan.get("running") and scan.get("ranAt") != before:
                item = _unmatched_row(listing, row["stored_path"])
                if item is None:
                    raise ActionError("Bindery's scan finished but did not list the file.")
                client.adopt_unmatched(int(item["id"]), book_id)
                if not _tracked(row):
                    raise ActionError("Bindery accepted the file but does not show it on the book yet.")
                break
            if time.monotonic() > deadline:
                raise ActionError("Bindery's library scan did not finish within 30 minutes.")
            time.sleep(FOLLOWUP_POLL_SECONDS)
    except Exception as exc:  # a background step: anything unexpected must still reach Activity
        message = (
            f"Needs you: the file is back on disk, but Bindery is not tracking it yet ({exc}). "
            "In Bindery, open Library → Unmatched and add it to the book."
        )
        set_cleanup_followup(put_back_id, message[:1000], status="attention")
        return message

    message = "Bindery is tracking the file again."
    if row.get("was_monitored"):
        try:
            client.set_book_monitored(book_id, True)
            message = "Bindery is tracking the file again and monitoring the book."
        except BinderyClientError as exc:
            message = (
                f"Needs you: Bindery is tracking the file again, but monitoring could not be "
                f"turned back on ({exc}). Turn it on for this book in Bindery."
            )
            set_cleanup_followup(put_back_id, message[:1000], status="attention")
            return message
    set_cleanup_followup(put_back_id, message)
    return message
