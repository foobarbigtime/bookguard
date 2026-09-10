from __future__ import annotations

import os
from pathlib import Path
import shutil
import time
from urllib.parse import quote

import requests

from .config import settings
from .db import (
    associations_inside_path,
    bindery_conn,
    bindery_file_by_id,
    create_cleanup_action,
    finish_cleanup_action,
    latest_scan,
)


class ActionError(RuntimeError):
    pass


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def resolve_bindery_api_key() -> str:
    """Return a configured key, or discover Bindery's persisted auth.api_key read-only."""
    configured = str(settings.bindery_api_key or "").strip()
    if configured:
        return configured

    try:
        with bindery_conn() as conn:
            tables = [
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            for table in tables:
                quoted = _quoted_identifier(table)
                columns = {
                    str(row["name"])
                    for row in conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                }
                if "key" not in columns or "value" not in columns:
                    continue
                row = conn.execute(
                    f"SELECT value FROM {quoted} WHERE key=? LIMIT 1",
                    ("auth.api_key",),
                ).fetchone()
                if row and row["value"]:
                    value = str(row["value"]).strip()
                    if value:
                        return value
    except Exception as exc:
        raise ActionError(f"Unable to read Bindery's stored API key: {exc}") from exc

    raise ActionError(
        "Bindery API key is unavailable. Configure BINDERY_API_KEY or ensure "
        "BookGuard can read Bindery's auth.api_key from the mounted database."
    )


def _require_actions() -> str:
    if not settings.allow_actions:
        raise ActionError("Actions are disabled. Set BOOKGUARD_ALLOW_ACTIONS=true to enable them.")
    return resolve_bindery_api_key()


def _delete_bindery_path(book_id: int, stored_path: str, api_key: str) -> None:
    url = f"{settings.bindery_url}/api/v1/book/{book_id}/file?path={quote(stored_path, safe='')}"
    try:
        response = requests.delete(
            url,
            headers={"X-Api-Key": api_key},
            timeout=30,
        )
    except Exception as exc:
        raise ActionError(f"Bindery detach request failed: {exc}") from exc
    if response.status_code >= 300:
        raise ActionError(f"Bindery detach failed ({response.status_code}): {response.text[:500]}")


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
    if not any(source == root or root in source.parents for root in allowed):
        raise ActionError("Refusing to quarantine a path outside configured media roots.")

    qroot = Path(settings.quarantine_root).resolve()
    qroot.mkdir(parents=True, exist_ok=True)
    dest = qroot / source.name
    if dest.exists():
        stem = source.stem
        suffix = source.suffix
        i = 2
        while True:
            candidate = qroot / f"{stem}-{i}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1
    return str(dest)


def quarantine(book_id: int, stored_path: str, local_path: str) -> str:
    _require_actions()
    if not os.path.exists(local_path):
        raise ActionError("The physical path is already missing.")

    linked = associations_inside_path(stored_path)
    if len(linked) != 1:
        raise ActionError(
            f"Refusing to move a shared path: Bindery has {len(linked)} associations inside it."
        )

    destination = _safe_quarantine_destination(local_path)
    detach(book_id, stored_path)
    try:
        shutil.move(local_path, destination)
    except Exception as exc:
        raise ActionError(
            "Bindery was detached, but moving the file/folder failed. Manual attention is required: "
            f"{exc}"
        ) from exc
    return destination
