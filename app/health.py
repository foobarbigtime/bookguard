"""Health checks for the System page and Home, arr-style.

Each check says what is wrong, why it matters, and how to fix it; a passing
check is listed briefly. Everything is read-only: files are opened read-only,
BookGuard's database gets a quick integrity check, the virus scanner gets the
same short probe System has always used, and Bindery's API is not called.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import os
import sqlite3
from typing import Any, Callable

from .config import settings
from .db import bindery_conn, latest_scan, local_conn, local_db_path

DEFINITIONS_MAX_AGE_DAYS = 3
RESTART_LOOKBACK_DAYS = 14
RESTART_MIN_NIGHTS = 3


def _check(key: str, level: str, title: str, message: str = "", fix: str = "") -> dict[str, Any]:
    return {"key": key, "level": level, "title": title, "message": message, "fix": fix}


def _bindery_database() -> dict[str, Any]:
    if not os.path.exists(settings.bindery_db):
        return _check(
            "bindery_db", "error", "BookGuard can't find Bindery's database",
            f"Nothing exists at {settings.bindery_db}, so BookGuard can't see your library or check imports.",
            "In Settings → Bindery connection, set the database path to where Bindery's appdata folder is "
            "mounted inside BookGuard, and mount that folder read-only in the container.",
        )
    try:
        with bindery_conn() as conn:
            conn.execute("SELECT 1 FROM book_files LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        return _check(
            "bindery_db", "error", "BookGuard can't read Bindery's database",
            f"The file exists but could not be read: {exc}.",
            "Check that the path points at Bindery's bindery.db and that BookGuard's user can read it.",
        )
    return _check("bindery_db", "ok", "Bindery database readable")


def _library_folders() -> list[dict[str, Any]]:
    out = []
    for key, label, root, wanted in (
        ("ebook_root", "Ebook library", settings.ebook_root, settings.scan_ebooks),
        ("audiobook_root", "Audiobook library", settings.audiobook_root, settings.scan_audiobooks),
    ):
        if not wanted:
            continue
        try:
            next(os.scandir(root), None)
            out.append(_check(key, "ok", f"{label} folder readable"))
        except OSError as exc:
            out.append(_check(
                key, "error", f"BookGuard can't read the {label.lower()} folder",
                f"{root}: {exc.strerror or exc}. Books there can't be scanned or verified.",
                f"Mount your {label.lower()} into the container at {root} (read-only), or change the path in "
                "Settings → Library paths.",
            ))
    return out


def _definitions_date(version: str) -> datetime | None:
    """ClamAV answers e.g. 'ClamAV 1.5.4/28141/Fri Oct  2 06:26:12 2026'."""
    parts = str(version or "").split("/")
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(" ".join(parts[2].split()), "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _virus_scanner(malware: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    if not malware.get("enabled"):
        return [_check("virus_scanner", "info", "Virus scanning is off",
                       "Files are checked for damage and identity but not scanned for viruses.",
                       "Deploy the optional ClamAV service (compose.clamav.yaml) and turn on Virus scanning "
                       "in Settings.")]
    if not malware.get("configured"):
        return [_check("virus_scanner", "warn", "Virus scanning is on, but no virus scanner is configured",
                       "Checks that need a virus scan can't finish.",
                       "Deploy compose.clamav.yaml and set BOOKGUARD_CLAMD_HOST, or turn off Virus scanning.")]
    if not malware.get("reachable"):
        return [_check("virus_scanner", "error", "The virus scanner is not answering",
                       str(malware.get("message") or "") + " Files can't be verified until it answers again.",
                       "Check that the ClamAV container is running (docker ps) and look at its log "
                       "(docker logs bookguard-clamav). It needs a few minutes after starting to load definitions.")]
    checks = [_check("virus_scanner", "ok", "Virus scanner answering")]
    issued = _definitions_date(str(malware.get("version") or ""))
    if issued is not None:
        age = now - issued
        if age > timedelta(days=DEFINITIONS_MAX_AGE_DAYS):
            checks.append(_check(
                "virus_definitions", "warn", "Virus definitions may not be updating",
                f"The newest definitions are from {issued:%b %d}, {age.days} days ago. They normally update "
                "several times a day, so recent threats could be missed.",
                "ClamAV's freshclam updates them and needs internet access. Check the ClamAV container's log "
                "for freshclam errors (docker logs bookguard-clamav).",
            ))
        else:
            checks.append(_check("virus_definitions", "ok", f"Virus definitions current ({issued:%b %d})"))
    return checks


def _bookguard_database() -> dict[str, Any]:
    try:
        uri = f"file:{local_db_path()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            answer = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
    except sqlite3.Error as exc:
        answer = str(exc)
    if answer == "ok":
        return _check("bookguard_db", "ok", "BookGuard database healthy")
    return _check("bookguard_db", "error", "BookGuard's database reports damage",
                  f"SQLite quick check answered: {answer}.",
                  "Stop BookGuard and restore the newest backup with scripts/bookguard-backup.sh "
                  "(see Backup and validation in the README).")


def _interrupted_scan(scan_running: Callable[[], bool]) -> dict[str, Any]:
    scan = latest_scan()
    if scan and scan.get("status") == "running" and not scan_running():
        return _check("interrupted_scan", "warn", "The last library scan was interrupted",
                      f"It stopped after {scan.get('processed') or 0} of {scan.get('total') or 0} books, usually "
                      "because BookGuard restarted while it ran. Review shows the results of that partial scan.",
                      "Start a new scan from Scheduled tasks below.")
    return _check("interrupted_scan", "ok", "No interrupted scan")


def nightly_restarts(starts: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """Restarts (same revision as the start before) clustered at one hour on several nights."""
    since = now - timedelta(days=RESTART_LOOKBACK_DAYS)
    nights: dict[int, set[str]] = defaultdict(set)
    for start in starts:
        try:
            at = datetime.fromisoformat(str(start["started_at"]))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at < since or not start.get("previous_revision") or start["revision"] != start["previous_revision"]:
            continue
        nights[at.hour].add(at.date().isoformat())
    if not nights:
        return None
    hour, days = max(nights.items(), key=lambda item: len(item[1]))
    neighbours = nights.get((hour + 1) % 24, set()) | nights.get((hour - 1) % 24, set())
    count = len(days | neighbours)
    if count < RESTART_MIN_NIGHTS:
        return None
    return {"hour": hour, "nights": count}


def _restarts(now: datetime) -> dict[str, Any]:
    with local_conn() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_starts'").fetchone()
        rows = [dict(r) for r in conn.execute("SELECT * FROM app_starts ORDER BY id")] if exists else []
    found = nightly_restarts(rows, now)
    if not found:
        return _check("restarts", "ok", "No repeated nightly restarts")
    return _check(
        "restarts", "warn", f"BookGuard is restarted most nights around {found['hour']:02d}:00 UTC",
        f"It restarted at that time on {found['nights']} nights in the last {RESTART_LOOKBACK_DAYS} days without "
        "an update. A restart cuts short any scan or check that is running.",
        "Something restarts the container on a schedule, often an automatic container updater such as "
        "Unraid's CA Auto Update. Exclude BookGuard from it, or schedule scans for another time.",
    )


def _import_checks() -> dict[str, Any] | None:
    from .import_watch import watcher

    status = watcher.status()
    if not status["enabled"]:
        return None
    if status["lastError"]:
        return _check("import_checks", "warn", "New imports can't be checked",
                      f"The last attempt failed: {status['lastError']}",
                      "This usually means Bindery's database can't be read; see that check above.")
    return _check("import_checks", "ok", "New imports are being checked")


def _uid() -> int:
    return os.getuid()


def _runtime() -> dict[str, Any]:
    if _uid() == 0:
        return _check("runtime_user", "warn", "BookGuard is running as root",
                      "A problem in BookGuard or a malicious file would have full control of the container.",
                      "Set BOOKGUARD_UID and BOOKGUARD_GID in .env (99 and 100 on Unraid) and redeploy.")
    return _check("runtime_user", "ok", "Running as a non-root user")


def health_checks(
    malware: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    scan_running: Callable[[], bool] | None = None,
    deep: bool = True,
) -> dict[str, Any]:
    """All checks, problems first, plus the passing ones as a short list.

    ``deep=False`` skips the database integrity check, which reads the whole
    database; Home uses that, the System page does not."""
    from .diagnostics import _malware_report
    from .scanner import scan_is_running

    now = now or datetime.now(timezone.utc)
    checks = [_bindery_database(), *_library_folders(), *_virus_scanner(malware or _malware_report(), now),
              *([_bookguard_database()] if deep else []),
              _interrupted_scan(scan_running or scan_is_running), _restarts(now)]
    imports = _import_checks()
    if imports:
        checks.append(imports)
    checks.append(_runtime())
    order = {"error": 0, "warn": 1, "info": 2, "ok": 3}
    problems = sorted((c for c in checks if c["level"] != "ok"), key=lambda c: order[c["level"]])
    return {
        "problems": problems,
        "passing": [c for c in checks if c["level"] == "ok"],
        "level": "error" if any(c["level"] == "error" for c in problems)
        else "warn" if any(c["level"] == "warn" for c in problems) else "ok",
    }


def system_about(malware: dict[str, Any]) -> list[dict[str, str]]:
    """Version, uptime, last update and the services BookGuard depends on."""
    from . import __version__
    from .change_log import current_revision
    from .config import ConfigurationError, load_automation_settings

    with local_conn() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_starts'").fetchone()
        latest = conn.execute("SELECT * FROM app_starts ORDER BY id DESC LIMIT 1").fetchone() if exists else None
        update = conn.execute(
            """SELECT * FROM app_starts WHERE previous_revision != '' AND revision != previous_revision
               ORDER BY id DESC LIMIT 1"""
        ).fetchone() if exists else None
    try:
        mode = load_automation_settings().automation_mode.capitalize()
    except ConfigurationError:
        mode = "Invalid settings"
    revision = current_revision()
    scanner = (str(malware.get("version") or "Answering") if malware.get("reachable")
               else "Not answering" if malware.get("configured") else "Not set up")
    return [
        {"label": "BookGuard", "text": f"v{__version__}" + (f" · {revision[:7]}" if revision != "unknown" else "")},
        {"label": "Running since", "text": "", "at": str(latest["started_at"]) if latest else ""},
        {"label": "Last updated", "text": "" if update else "No update recorded yet",
         "at": str(update["started_at"]) if update else ""},
        {"label": "Virus scanner", "text": scanner},
        {"label": "Automation", "text": mode},
    ]
