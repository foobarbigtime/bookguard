"""Check each file Bindery imports soon after it appears.

A full library scan finds a wrong or damaged import only the next time it
runs. The watcher looks at Bindery's database (read-only, as the scan does)
every few minutes, or straight away when Bindery's import webhook nudges it,
and checks only the files added since it last looked:

1. the file is scanned exactly as a full scan would scan it, and the result is
   added to the latest scan, so it appears in Review and on Home;
2. an ebook the scan flags is verified (identity and safety checks);
3. the outcome is recorded for Activity.

It starts from "now" the first time it runs (the full scan covers what is
already there), waits while a full scan is running, and changes nothing in
Bindery or the library.

Bindery's import webhook carries no file path for ebooks, so it is only a
nudge: the database decides which files are new. Bindery blocks webhooks to
private addresses unless BINDERY_NOTIFICATIONS_ALLOW_PRIVATE is set, so the
timer, not the webhook, is what makes this work out of the box.
"""

from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from .config import settings
from .db import (
    add_result,
    bindery_files_after,
    bindery_max_file_id,
    latest_scan,
    local_conn,
    result_by_id,
    utc_now,
)

BATCH = 50
_VERIFY_CLASSES = {"REVIEW", "REJECT"}


def init_import_watch_db() -> None:
    with local_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS import_watch_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS import_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                format TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                result_id INTEGER,
                classification TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                trigger TEXT NOT NULL,
                checked_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_import_checks_file ON import_checks(file_id);
            """
        )
        conn.commit()


def _watermark() -> int | None:
    with local_conn() as conn:
        row = conn.execute("SELECT value FROM import_watch_state WHERE key='last_file_id'").fetchone()
    return int(row["value"]) if row else None


def _set_watermark(file_id: int) -> None:
    with local_conn() as conn:
        conn.execute(
            """INSERT INTO import_watch_state(key, value) VALUES ('last_file_id', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(int(file_id)),),
        )
        conn.commit()


def _result_for(scan_id: str, file_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute(
            "SELECT id FROM scan_results WHERE scan_id=? AND file_id=? ORDER BY id DESC LIMIT 1",
            (scan_id, int(file_id)),
        ).fetchone()
    return result_by_id(int(row["id"])) if row else None


def _record(row: dict, trigger: str, result: dict | None, verdict: str = "", error: str = "") -> None:
    with local_conn() as conn:
        conn.execute(
            """INSERT INTO import_checks(file_id, book_id, format, title, author, stored_path, result_id,
                   classification, verdict, error, trigger, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(row["file_id"]), int(row["book_id"]), str(row["format"]), str(row["title"] or ""),
                str(row["author"] or ""), str(row["stored_path"]), int(result["id"]) if result else None,
                str((result or {}).get("classification") or ""), verdict, error[:500], trigger, utc_now(),
            ),
        )
        conn.commit()


def _wanted(row: dict) -> bool:
    if row["format"] == "audiobook":
        return settings.scan_audiobooks
    return settings.scan_ebooks


def check_new_imports(trigger: str = "timer") -> dict[str, Any]:
    """Scan and verify files Bindery added since the last check. Never raises."""
    from .scanner import _scan_one, scan_is_running
    from .verifier import verify_result

    if not settings.watch_imports:
        return {"state": "off", "checked": 0}
    if scan_is_running():
        return {"state": "waiting_for_scan", "checked": 0}
    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        return {"state": "waiting_for_first_scan", "checked": 0}

    init_import_watch_db()
    watermark = _watermark()
    if watermark is None:
        # First run: start from now; the full scan already covered the rest.
        _set_watermark(bindery_max_file_id())
        return {"state": "started", "checked": 0}

    checked = 0
    for row in bindery_files_after(watermark, BATCH):
        if scan_is_running():
            break
        if _wanted(row) and _result_for(scan["id"], row["file_id"]) is None:
            try:
                add_result(scan["id"], _scan_one(row))
                result = _result_for(scan["id"], row["file_id"])
                verdict = ""
                if (
                    result
                    and result["format"] == "ebook"
                    and result["classification"] in _VERIFY_CLASSES
                    and settings.verification_enabled
                ):
                    verdict = str(verify_result(result).get("verdict") or "")
                _record(row, trigger, result, verdict)
            except Exception as exc:  # noqa: BLE001 - recorded and shown in Activity
                _record(row, trigger, None, error=str(exc) or exc.__class__.__name__)
            checked += 1
        _set_watermark(int(row["file_id"]))
    return {"state": "checked", "checked": checked}


class ImportWatcher:
    """Runs check_new_imports on a timer, or at once when nudged."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict[str, Any] = {"lastCheckAt": None, "lastState": None, "lastChecked": 0, "lastError": None}

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="bookguard-import-watch", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def nudge(self) -> None:
        self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = deepcopy(self._status)
        snapshot["enabled"] = settings.watch_imports
        snapshot["minutes"] = settings.watch_imports_minutes
        return snapshot

    def run_once(self, trigger: str) -> dict[str, Any]:
        try:
            outcome = check_new_imports(trigger)
            error = None
        except Exception as exc:  # noqa: BLE001 - e.g. Bindery's database is unreadable
            outcome, error = {"state": "error", "checked": 0}, str(exc)[:500]
        with self._lock:
            self._status.update(lastCheckAt=utc_now(), lastState=outcome["state"],
                                lastChecked=outcome["checked"], lastError=error)
        return outcome

    def _run(self) -> None:
        trigger = "timer"
        while not self._stop.is_set():
            self.run_once(trigger)
            woke = self._wake.wait(timeout=max(60, settings.watch_imports_minutes * 60))
            self._wake.clear()
            trigger = "webhook" if woke else "timer"


watcher = ImportWatcher()
