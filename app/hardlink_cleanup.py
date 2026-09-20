from __future__ import annotations

import json
import os
from pathlib import Path

from .action_paths import ebook_action_preview, resolve_writable_ebook_path
from .actions import ActionError
from .bindery_client import BinderyClient
from .config import load_automation_settings, settings
from .db import local_conn, utc_now
from .ebook_security import inspect_ebook_security
from .file_safety import sha256_file
from .hardlink_conflicts import (
    _POLICY, _init_journal, _lock, _require_idle,
    _require_no_unresolved, _token, _verify_postconditions,
)


def _record(correction_id: int) -> dict:
    _init_journal()
    with local_conn() as conn:
        row = conn.execute("SELECT * FROM hardlink_corrections WHERE id=?",
                           (correction_id,)).fetchone()
    if not row or row["status"] != "applied":
        raise ActionError("An applied hard-link correction record is required for cleanup.")
    return json.loads(row["snapshot_json"])


def _require_no_pending_cleanup() -> None:
    with local_conn() as conn:
        pending = conn.execute(
            "SELECT correction_id FROM hardlink_alias_cleanups "
            "WHERE status NOT IN ('applied', 'cancelled') LIMIT 1"
        ).fetchone()
    if pending:
        raise ActionError("An interrupted alias cleanup must be reconciled before another cleanup.")


def _cleanup_snapshot(correction_id: int, client: BinderyClient) -> dict:
    original = _record(correction_id)
    if not all(getattr(settings, key) for key in _POLICY):
        raise ActionError("All core ebook verification checks must be enabled.")
    _require_idle(client)
    _require_no_unresolved()
    _require_no_pending_cleanup()
    if (not Path(original["source"]).name.startswith(".bindery-stage-")
            or Path(original["source"]).parent != Path(original["destination"]).parent
            or original["fingerprint"]["links"] != 2):
        raise ActionError("The correction record is not an eligible staging hard-link proof.")
    # Checks absence of the wrong association across all books, both books'
    # complete file registrations, exact paths, inode, link count, and full hash.
    _verify_postconditions(original, client)
    if not inspect_ebook_security(original["destination"])["safe"]:
        raise ActionError("The retained EPUB failed deterministic safety validation.")
    action_root = load_automation_settings().ebook_action_root
    return {"correctionId": correction_id, "correctionSnapshot": original,
            "actionRoot": action_root}


def cleanup_preview(correction_id: int, client: BinderyClient | None = None) -> dict:
    try:
        proof = _cleanup_snapshot(correction_id, client or BinderyClient())
        original = proof["correctionSnapshot"]
        action = ebook_action_preview(original["source"], original["wrong"]["stored_path"])
        return {"safe": True, "ready": action["ready"], "token": _token(proof),
                "correctionId": correction_id, "alias": original["source"],
                "retained": original["destination"], "sha256": original["sha256"],
                "action": action,
                "reason": ("The unregistered staging name is safe to remove."
                           if action["ready"] else "Evidence passed; writable ebook action gates are not ready.")}
    except Exception as exc:
        return {"safe": False, "ready": False, "correctionId": correction_id, "reason": str(exc)}


def _unlink_exact_alias(original: dict) -> None:
    alias = resolve_writable_ebook_path(original["source"], original["wrong"]["stored_path"])
    root = Path(load_automation_settings().ebook_action_root)
    relative = alias.relative_to(root)
    # Walk from the approved root with directory handles and no symlink
    # following, so unlink cannot be redirected through a parent symlink.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            expected = original["fingerprint"]
            actual = {"device": info.st_dev, "inode": info.st_ino, "links": info.st_nlink,
                      "bytes": info.st_size, "mtimeNs": info.st_mtime_ns, "ctimeNs": info.st_ctime_ns}
            if actual != expected or sha256_file(alias) != original["sha256"]:
                raise ActionError("The writable staging alias changed before unlink.")
            current = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ActionError("The staging directory entry changed before unlink.")
            # Only this exact name is unlinked. No directory removal or sibling sweep.
            os.unlink(relative.name, dir_fd=parent_fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def cleanup_staging_alias(correction_id: int, token: str,
                          client: BinderyClient | None = None) -> dict:
    if not settings.allow_actions:
        raise ActionError("Bindery actions are disabled; staging cleanup is blocked.")
    if not _lock.acquire(blocking=False):
        raise ActionError("Another hard-link operation is running.")
    journaled = False
    try:
        client = client or BinderyClient()
        proof = _cleanup_snapshot(correction_id, client)
        if not token or _token(proof) != token:
            raise ActionError("The cleanup preview changed. Inspect a fresh preview.")
        original = proof["correctionSnapshot"]
        # Require an explicit opt-in action alias; never make /books writable.
        resolve_writable_ebook_path(original["source"], original["wrong"]["stored_path"])
        with local_conn() as conn:
            previous = conn.execute("SELECT status FROM hardlink_alias_cleanups WHERE correction_id=?",
                                    (correction_id,)).fetchone()
            if previous and previous["status"] != "cancelled":
                raise ActionError("This cleanup already has a journal entry; recheck it instead of repeating it.")
            conn.execute(
                "INSERT INTO hardlink_alias_cleanups(correction_id,snapshot_json,status,created_at) "
                "VALUES (?,?,'running',?) ON CONFLICT(correction_id) DO UPDATE SET "
                "snapshot_json=excluded.snapshot_json,status='running',created_at=excluded.created_at,"
                "completed_at=NULL,error=NULL",
                (correction_id, json.dumps(proof, sort_keys=True), utc_now()),
            )
            conn.commit()
            journaled = True
        # The operation's own running row is intentional; recheck the remaining
        # proof directly immediately before the filesystem mutation.
        _require_idle(client)
        _require_no_unresolved()
        if not all(getattr(settings, key) for key in _POLICY):
            raise ActionError("Core verification settings changed before staging cleanup.")
        _verify_postconditions(original, client)
        _unlink_exact_alias(original)
        _verify_postconditions(original, client, removed_alias=True)
        with local_conn() as conn:
            conn.execute("UPDATE hardlink_alias_cleanups SET status='applied',completed_at=? "
                         "WHERE correction_id=?", (utc_now(), correction_id))
            conn.commit()
        return {"ok": True, "correctionId": correction_id, "aliasRemoved": True,
                "retainedBytesChanged": False,
                "message": "Unregistered staging link removed. The final EPUB and audiobook registrations were preserved."}
    except Exception as exc:
        if journaled:
            with local_conn() as conn:
                conn.execute("UPDATE hardlink_alias_cleanups SET status='needs_review',completed_at=?,error=? "
                             "WHERE correction_id=?", (utc_now(), str(exc)[:1000], correction_id))
                conn.commit()
        if isinstance(exc, ActionError):
            raise
        raise ActionError(str(exc)) from exc
    finally:
        _lock.release()


def reconcile_alias_cleanup(correction_id: int, client: BinderyClient | None = None) -> dict:
    """Observe an interrupted cleanup; never repeat unlink or recreate a link."""
    if not _lock.acquire(blocking=False):
        raise ActionError("Another hard-link operation is running.")
    try:
        _init_journal()
        with local_conn() as conn:
            row = conn.execute("SELECT * FROM hardlink_alias_cleanups WHERE correction_id=?",
                               (correction_id,)).fetchone()
        if not row:
            raise ActionError("Cleanup record not found.")
        if row["status"] in {"applied", "cancelled"}:
            return {"ok": True, "status": row["status"], "message": "Cleanup already reconciled."}
        original = json.loads(row["snapshot_json"])["correctionSnapshot"]
        client = client or BinderyClient()
        _require_idle(client)
        source = Path(original["source"])
        absent = not source.exists() and not source.is_symlink()
        _verify_postconditions(original, client, removed_alias=absent)
        status = "applied" if absent else "cancelled"
        with local_conn() as conn:
            conn.execute("UPDATE hardlink_alias_cleanups SET status=?,completed_at=?,error=NULL "
                         "WHERE correction_id=?", (status, utc_now(), correction_id))
            conn.commit()
        return {"ok": True, "status": status,
                "message": "Cleanup state verified without modifying files or registrations."}
    except Exception as exc:
        if isinstance(exc, ActionError):
            raise
        raise ActionError(str(exc)) from exc
    finally:
        _lock.release()
