"""Durable records of setting changes and BookGuard starts, shown in Activity.

Two things the operator asked about after the fact had no record: "who changed
this setting, and from what?" and "when did BookGuard update?". Settings saves
and resets now record exactly which values changed (secrets only as
"changed"), and every start records the running version and Git revision, so
a start on a new revision appears as an update.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import __version__
from .db import local_conn, utc_now

# Settings whose names alone do not say what they are.
SETTING_LABELS = {
    "allow_actions": "Bindery actions",
    "author_aliases": "Pen names",
    "bindery_api_key_set": "Bindery API key",
    "bindery_db": "Bindery database path",
    "bindery_url": "Bindery URL",
    "metadata_repair_mode": "Metadata repair mode",
    "music_genres": "Music genres",
    "scan_on_start": "Scan when BookGuard starts",
    "verification_malware_scan": "Virus scanning",
    "verification_use_tika": "Tika text extraction",
    "watch_imports": "Check each new Bindery import",
    "watch_imports_minutes": "Minutes between import checks",
    "scan_schedule": "Scheduled library scan",
    "scan_schedule_time": "Scheduled scan time",
    "scan_schedule_day": "Weekly scan day",
}


def setting_label(key: str) -> str:
    return SETTING_LABELS.get(key) or key.replace("_", " ").capitalize()


def init_change_log_db() -> None:
    with local_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                changed_at TEXT NOT NULL,
                action TEXT NOT NULL,
                changes_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_starts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                version TEXT NOT NULL,
                revision TEXT NOT NULL,
                previous_version TEXT NOT NULL DEFAULT '',
                previous_revision TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.commit()


def settings_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Changed public settings. List settings report what was added and removed."""
    changes = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        change: dict[str, Any] = {"key": key, "label": setting_label(key)}
        if isinstance(old, list) or isinstance(new, list):
            old_items, new_items = list(old or []), list(new or [])
            change["added"] = [item for item in new_items if item not in old_items]
            change["removed"] = [item for item in old_items if item not in new_items]
        else:
            change["before"], change["after"] = old, new
        changes.append(change)
    return changes


def record_settings_change(before: dict[str, Any], after: dict[str, Any], action: str = "save") -> int | None:
    """Record a settings save or reset; nothing is recorded when nothing changed."""
    changes = settings_diff(before, after)
    if not changes:
        return None
    init_change_log_db()
    with local_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO settings_changes(changed_at, action, changes_json) VALUES (?, ?, ?)",
            (utc_now(), action, json.dumps(changes)),
        )
        conn.commit()
        return int(cursor.lastrowid)


def current_revision() -> str:
    return os.getenv("BOOKGUARD_BUILD_REVISION", "").strip() or "unknown"


def record_app_start() -> dict[str, Any]:
    """Record this start, with the previous start's version and revision."""
    init_change_log_db()
    with local_conn() as conn:
        previous = conn.execute(
            "SELECT version, revision FROM app_starts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        row = {
            "started_at": utc_now(),
            "version": __version__,
            "revision": current_revision(),
            "previous_version": str(previous["version"]) if previous else "",
            "previous_revision": str(previous["revision"]) if previous else "",
        }
        conn.execute(
            """INSERT INTO app_starts(started_at, version, revision, previous_version, previous_revision)
               VALUES (:started_at, :version, :revision, :previous_version, :previous_revision)""",
            row,
        )
        conn.commit()
    return row
