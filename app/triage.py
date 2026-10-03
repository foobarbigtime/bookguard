from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

from .action_paths import ebook_action_preview, mount_is_writable
from .actions import ActionError, detach, quarantine_file
from .config import settings
from .db import (
    bindery_file_by_id,
    create_cleanup_action,
    finish_cleanup_action,
    latest_results,
    latest_scan,
    local_conn,
    record_cleanup_location,
)
from .file_safety import sha256_file


TRIAGE_CLASSES = {"REVIEW", "REJECT"}


def init_triage_db() -> None:
    with local_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS triage_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL UNIQUE,
                result_id INTEGER NOT NULL,
                scan_id TEXT NOT NULL,
                file_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                classification TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                format TEXT NOT NULL,
                author TEXT NOT NULL,
                title TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                decision TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_triage_decisions_updated
                ON triage_decisions(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_triage_decisions_file
                ON triage_decisions(file_id);
            """
        )
        conn.commit()


def result_signature(result: dict) -> str:
    payload = {
        "file_id": result.get("file_id"),
        "book_id": result.get("book_id"),
        "format": result.get("format"),
        "stored_path": result.get("stored_path"),
        "classification": result.get("classification"),
        "reason_code": result.get("reason_code") or "UNKNOWN",
        "author": result.get("author"),
        "title": result.get("title"),
        "reasons": result.get("reasons") or [],
        "metadata": result.get("metadata") or {},
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _require_current_triage_result(result: dict) -> None:
    if result.get("classification") not in TRIAGE_CLASSES:
        raise ActionError("Only REVIEW and REJECT results can use the triage workflow.")

    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise ActionError("A completed latest scan is required for triage actions.")
    if scan.get("id") != result.get("scan_id"):
        raise ActionError("This result is not from the latest completed scan. Refresh triage first.")


def triage_decision_for_result(result: dict) -> dict | None:
    init_triage_db()
    signature = result_signature(result)
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM triage_decisions WHERE signature=? LIMIT 1",
            (signature,),
        ).fetchone()
    return dict(row) if row else None


def save_keep_decision(result: dict) -> int:
    _require_current_triage_result(result)
    init_triage_db()
    signature = result_signature(result)
    now = _utc_now()
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO triage_decisions(
                signature, result_id, scan_id, file_id, book_id, classification,
                reason_code, format, author, title, stored_path, decision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'KEEP', ?, ?)
            ON CONFLICT(signature) DO UPDATE SET
                result_id=excluded.result_id,
                scan_id=excluded.scan_id,
                decision='KEEP',
                updated_at=excluded.updated_at
            """,
            (
                signature,
                result["id"],
                result["scan_id"],
                result["file_id"],
                result["book_id"],
                result["classification"],
                result.get("reason_code") or "UNKNOWN",
                result["format"],
                result["author"],
                result["title"],
                result["stored_path"],
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM triage_decisions WHERE signature=?",
            (signature,),
        ).fetchone()
        conn.commit()
    return int(row["id"])


def clear_keep_decision(result: dict) -> bool:
    _require_current_triage_result(result)
    init_triage_db()
    signature = result_signature(result)
    with local_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM triage_decisions WHERE signature=?",
            (signature,),
        )
        conn.commit()
    return bool(cursor.rowcount)


def _latest_applied_cleanup(result_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM cleanup_actions
            WHERE result_id=?
              AND status='applied'
              AND action_kind IN ('TRIAGE_DETACH', 'TRIAGE_QUARANTINE')
            ORDER BY id DESC
            LIMIT 1
            """,
            (result_id,),
        ).fetchone()
    return dict(row) if row else None


def triage_state(result: dict) -> dict:
    cleanup = _latest_applied_cleanup(int(result["id"]))
    if cleanup:
        resolution = "DETACHED" if cleanup["action_kind"] == "TRIAGE_DETACH" else "QUARANTINED"
        return {
            "resolved": True,
            "resolution": resolution,
            "decision_id": None,
            "cleanup_id": cleanup["id"],
        }

    decision = triage_decision_for_result(result)
    if decision:
        return {
            "resolved": True,
            "resolution": decision["decision"],
            "decision_id": decision["id"],
            "cleanup_id": None,
        }

    return {
        "resolved": False,
        "resolution": "OPEN",
        "decision_id": None,
        "cleanup_id": None,
    }


def triage_summary() -> dict:
    out = {
        "REVIEW": {"total": 0, "open": 0, "resolved": 0},
        "REJECT": {"total": 0, "open": 0, "resolved": 0},
    }
    for classification in ("REVIEW", "REJECT"):
        rows = latest_results(classification=classification, limit=10000)
        out[classification]["total"] = len(rows)
        for row in rows:
            state = triage_state(row)
            if state["resolved"]:
                out[classification]["resolved"] += 1
            else:
                out[classification]["open"] += 1
    return out


def _exact_bindery_match(result: dict) -> bool:
    row = bindery_file_by_id(int(result["file_id"]))
    return bool(
        row
        and row["book_id"] == result["book_id"]
        and row["format"] == result["format"]
        and row["stored_path"] == result["stored_path"]
    )


def triage_action_preview(result: dict, action: str) -> dict:
    _require_current_triage_result(result)
    exact = _exact_bindery_match(result)
    physical_exists = os.path.exists(result.get("local_path") or "")

    if action == "detach":
        safe = exact
        reason = (
            "The exact Bindery file association still matches this scan. Detach removes only "
            "that association; the physical file is left in place."
            if safe
            else "The Bindery association changed since the scan. Refresh before detaching."
        )
    elif action == "quarantine":
        ebook_action = None
        writable_source = True
        if str(result.get("format") or "").lower() == "ebook" and physical_exists:
            ebook_action = ebook_action_preview(
                str(result.get("local_path") or ""),
                str(result.get("stored_path") or ""),
            )
            writable_source = bool(ebook_action["ready"])
        elif physical_exists:
            writable_source = mount_is_writable(str(result.get("local_path") or ""))

        safe = exact and physical_exists and writable_source
        if not exact:
            reason = "The Bindery association changed since the scan. Refresh before quarantine."
        elif not physical_exists:
            reason = "The physical path is no longer present. Quarantine is not appropriate."
        elif ebook_action and not ebook_action["ready"]:
            reason = (
                "Writable ebook action preflight failed: "
                + ", ".join(ebook_action["blockers"])
            )
        elif not writable_source:
            reason = "The source media mount is read-only."
        else:
            reason = "The association still matches and the physical path exists."
    else:
        raise ActionError("Unknown triage action.")

    return {
        "eligible": safe,
        "safe": safe,
        "action": action,
        "reason": reason,
        "exact_db_match": exact,
        "physical_exists": physical_exists,
        "stored_path": result["stored_path"],
        "local_path": result["local_path"],
        "ebook_action": ebook_action if action == "quarantine" else None,
    }


def _wait_for_bindery_row_gone(file_id: int) -> None:
    for _ in range(20):
        if bindery_file_by_id(file_id) is None:
            return
        time.sleep(0.2)
    raise ActionError(f"Bindery file association {file_id} still exists after the detach request.")


def triage_detach(result: dict) -> int:
    if not settings.allow_actions:
        raise ActionError("Bindery actions are disabled. Enable actions in Settings before detaching REVIEW/REJECT items.")

    preview = triage_action_preview(result, "detach")
    if not preview["safe"]:
        raise ActionError(preview["reason"])

    cleanup_id = create_cleanup_action(result, "TRIAGE_DETACH")
    try:
        final_preview = triage_action_preview(result, "detach")
        if not final_preview["safe"]:
            raise ActionError(final_preview["reason"])
        detach(result["book_id"], result["stored_path"])
        _wait_for_bindery_row_gone(int(result["file_id"]))
        finish_cleanup_action(cleanup_id, "applied")
        return cleanup_id
    except Exception as exc:
        finish_cleanup_action(cleanup_id, "failed", str(exc)[:1000])
        if isinstance(exc, ActionError):
            raise
        raise ActionError(str(exc)) from exc


def _record_quarantine_location(cleanup_id: int, original: Path, destination: Path) -> None:
    """Remember where the file went and its bytes, so Put back can undo exactly this."""
    sha256 = size_bytes = None
    try:
        if destination.is_file() and not destination.is_symlink():
            sha256 = sha256_file(destination)
            size_bytes = destination.stat().st_size
    except OSError:
        sha256 = size_bytes = None  # the quarantine still stands; it just cannot be put back
    record_cleanup_location(
        cleanup_id,
        original_path=str(original),
        quarantine_path=str(destination),
        sha256=sha256,
        size_bytes=size_bytes,
    )


def triage_quarantine(result: dict) -> tuple[int, str]:
    if not settings.allow_actions:
        raise ActionError("Bindery actions are disabled. Enable actions in Settings before quarantining REVIEW/REJECT items.")

    preview = triage_action_preview(result, "quarantine")
    if not preview["safe"]:
        raise ActionError(preview["reason"])

    if not mount_is_writable(settings.quarantine_root):
        raise ActionError("The quarantine mount is not writable.")

    cleanup_id = create_cleanup_action(result, "TRIAGE_QUARANTINE")
    try:
        final_preview = triage_action_preview(result, "quarantine")
        if not final_preview["safe"]:
            raise ActionError(final_preview["reason"])
        destination, original = quarantine_file(
            result["book_id"],
            result["stored_path"],
            result["local_path"],
        )
        _record_quarantine_location(cleanup_id, original, Path(destination))
        _wait_for_bindery_row_gone(int(result["file_id"]))
        finish_cleanup_action(cleanup_id, "applied")
        return cleanup_id, destination
    except Exception as exc:
        finish_cleanup_action(cleanup_id, "failed", str(exc)[:1000])
        if isinstance(exc, ActionError):
            raise
        raise ActionError(str(exc)) from exc
