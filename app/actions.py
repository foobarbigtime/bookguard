from __future__ import annotations

import os
from pathlib import Path
import time

from .action_paths import EbookActionSafetyError, mount_is_writable, resolve_writable_ebook_path
from .bindery_client import BinderyClient, BinderyClientError, discover_api_key
from .config import settings
from .db import (
    associations_inside_path,
    bindery_file_by_id,
    create_cleanup_action,
    finish_cleanup_action,
    latest_scan,
)
from .file_safety import allocate_unique_destination, is_within, roots_overlap
from .quarantine_fs import (
    QuarantineCommitError,
    QuarantineMoveError,
    commit_quarantine_or_rollback,
    move_to_quarantine,
)


class ActionError(RuntimeError):
    pass


def resolve_bindery_api_key() -> str:
    """Return a configured key, or discover Bindery's persisted auth.api_key read-only."""
    try:
        return discover_api_key()
    except BinderyClientError as exc:
        raise ActionError(str(exc)) from exc


def _require_actions() -> str:
    if not settings.allow_actions:
        raise ActionError("Actions are disabled. Set BOOKGUARD_ALLOW_ACTIONS=true to enable them.")
    return resolve_bindery_api_key()


def _delete_bindery_path(book_id: int, stored_path: str, api_key: str) -> None:
    try:
        BinderyClient(api_key=api_key, timeout=30).deregister_file(book_id, stored_path)
    except BinderyClientError as exc:
        raise ActionError(str(exc)) from exc


def detach(book_id: int, stored_path: str) -> None:
    api_key = _require_actions()
    _delete_bindery_path(book_id, stored_path, api_key)


def missing_detach_preview(result: dict) -> dict:
    """Recheck whether one MISSING result is safe for stale-association detach."""
    if result.get("classification") != "MISSING":
        return {
            "eligible": False,
            "safe": False,
            "state": "not_missing",
            "reason": "Only MISSING results can use stale-association cleanup.",
        }

    scan = latest_scan()
    same_scan = bool(
        scan
        and scan.get("status") == "complete"
        and scan.get("id") == result.get("scan_id")
    )
    physical_exists = os.path.exists(result.get("local_path") or "")
    row = bindery_file_by_id(int(result["file_id"]))
    exact_db_match = bool(
        row
        and row["book_id"] == result["book_id"]
        and row["format"] == result["format"]
        and row["stored_path"] == result["stored_path"]
    )

    already_detached = row is None and not physical_exists
    safe = same_scan and not physical_exists and exact_db_match

    if safe:
        state = "safe"
        reason = (
            "Physical path is still absent and the exact Bindery file association "
            "still matches this completed scan."
        )
    elif already_detached:
        state = "already_detached"
        reason = "The Bindery association is already gone; run a fresh scan to clear this stale result."
    elif physical_exists:
        state = "path_exists"
        reason = "The physical path now exists, so BookGuard refuses stale-association cleanup."
    elif not same_scan:
        state = "scan_changed"
        reason = "The result is not from the latest completed scan. Run a fresh scan before cleanup."
    else:
        state = "association_changed"
        reason = "The Bindery file association no longer exactly matches this scan result."

    return {
        "eligible": safe,
        "safe": safe,
        "state": state,
        "reason": reason,
        "physical_exists": physical_exists,
        "exact_db_match": exact_db_match,
        "file_id": result["file_id"],
        "book_id": result["book_id"],
        "stored_path": result["stored_path"],
        "local_path": result["local_path"],
    }


def detach_missing(result: dict) -> int:
    """Detach one proven-stale MISSING association without touching physical media."""
    preview = missing_detach_preview(result)
    if not preview.get("safe"):
        raise ActionError(preview.get("reason") or "MISSING result is not safe to detach.")

    api_key = resolve_bindery_api_key()
    cleanup_id = create_cleanup_action(result, "DETACH_MISSING")

    try:
        # Recheck immediately before the API mutation.
        final_preview = missing_detach_preview(result)
        if not final_preview.get("safe"):
            raise ActionError(final_preview.get("reason") or "Safety state changed before detach.")

        _delete_bindery_path(result["book_id"], result["stored_path"], api_key)

        # Bindery's database can update a fraction of a second after the HTTP response.
        gone = False
        for _ in range(20):
            if bindery_file_by_id(int(result["file_id"])) is None:
                gone = True
                break
            time.sleep(0.2)
        if not gone:
            raise ActionError(
                f"Bindery reported success but file association {result['file_id']} still exists."
            )

        # This workflow must never create, delete, move, or rewrite media.
        if os.path.exists(result["local_path"]):
            raise ActionError(
                "The physical path appeared during cleanup. The Bindery association was detached, "
                "but the filesystem now requires manual review."
            )

        finish_cleanup_action(cleanup_id, "applied")
        return cleanup_id
    except Exception as exc:
        finish_cleanup_action(cleanup_id, "failed", str(exc)[:1000])
        if isinstance(exc, ActionError):
            raise
        raise ActionError(str(exc)) from exc


def _safe_quarantine_destination(local_path: str) -> str:
    source = Path(local_path).resolve()
    allowed = [Path(settings.audiobook_root).resolve(), Path(settings.ebook_root).resolve()]
    if not any(is_within(source, root) for root in allowed):
        raise ActionError("Refusing to quarantine a path outside configured media roots.")

    qroot = Path(settings.quarantine_root).resolve()
    if any(roots_overlap(qroot, root) for root in allowed):
        raise ActionError("Quarantine root must be outside configured media roots.")
    try:
        return str(allocate_unique_destination(qroot, source))
    except RuntimeError as exc:
        raise ActionError(str(exc)) from exc


def quarantine(book_id: int, stored_path: str, local_path: str) -> str:
    return quarantine_file(book_id, stored_path, local_path)[0]


def quarantine_file(book_id: int, stored_path: str, local_path: str) -> tuple[str, Path]:
    """Quarantine one path; return the quarantine destination and the path it left."""
    api_key = _require_actions()
    if not os.path.exists(local_path):
        raise ActionError("The physical path is already missing.")

    linked = associations_inside_path(stored_path)
    if len(linked) != 1:
        raise ActionError(
            f"Refusing to move a shared path: Bindery has {len(linked)} associations inside it."
        )

    destination = _safe_quarantine_destination(local_path)
    read_source = Path(local_path).resolve()
    ebook_root = Path(settings.ebook_root).resolve()
    if is_within(read_source, ebook_root):
        try:
            mutation_source = resolve_writable_ebook_path(local_path, stored_path)
        except EbookActionSafetyError as exc:
            raise ActionError(str(exc)) from exc
    else:
        mutation_source = read_source
        if not mount_is_writable(mutation_source):
            raise ActionError("The source media mount is read-only.")

    destination_path = Path(destination)
    try:
        move_to_quarantine(
            mutation_source,
            read_source,
            destination_path,
        )
    except QuarantineMoveError as exc:
        raise ActionError(
            f"Quarantine move failed before Bindery was changed: {exc}"
        ) from exc

    try:
        commit_quarantine_or_rollback(
            lambda: _delete_bindery_path(book_id, stored_path, api_key),
            mutation_source,
            read_source,
            destination_path,
        )
    except QuarantineCommitError as exc:
        rollback_error = (
            f" Rollback also failed: {exc.rollback_error}"
            if exc.rollback_error is not None
            else ""
        )
        raise ActionError(
            f"Bindery detach failed after quarantine: {exc.cause}.{rollback_error}"
        ) from exc
    return destination, mutation_source
