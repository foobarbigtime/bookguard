"""Scheduled tasks: an optional library scan on a schedule, and the task list.

The library scan can run daily or weekly at a set time (container local time),
off by default. A slot missed because BookGuard was stopped still runs when it
comes back, as long as it is less than a few hours late; an older one waits
for the next slot rather than starting a long scan at an unexpected time.

The task list shows every recurring job on the System page with when it last
ran and when it runs next: the scheduled scan, the import check (its own
timer, see import_watch), and the Observe check (run by hand).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo
import os
import threading
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings
from .db import latest_scan, local_conn

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SCHEDULES = {"off", "daily", "weekly"}
LATE_LIMIT = timedelta(hours=3)


def local_zone() -> tzinfo:
    """The container's time zone as a real zone, so slots follow daylight saving.

    datetime.now().astimezone() gives only today's fixed offset; slots a week
    away computed from it would be an hour off across a clock change."""
    name = os.getenv("TZ", "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def local_now() -> datetime:
    return datetime.now(local_zone())


def init_scheduler_db() -> None:
    with local_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS scheduler_state (task TEXT PRIMARY KEY, last_started_at TEXT NOT NULL)"
        )
        conn.commit()


def _last_started(task: str) -> datetime | None:
    with local_conn() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_state'").fetchone()
        row = conn.execute("SELECT last_started_at FROM scheduler_state WHERE task=?", (task,)).fetchone() if exists else None
    return datetime.fromisoformat(row["last_started_at"]) if row else None


def _set_last_started(task: str, at: datetime) -> None:
    init_scheduler_db()
    with local_conn() as conn:
        conn.execute(
            """INSERT INTO scheduler_state(task, last_started_at) VALUES (?, ?)
               ON CONFLICT(task) DO UPDATE SET last_started_at=excluded.last_started_at""",
            (task, at.isoformat()),
        )
        conn.commit()


def _scan_time() -> time:
    try:
        hours, minutes = (int(part) for part in str(settings.scan_schedule_time).split(":", 1))
        return time(hours % 24, minutes % 60)
    except ValueError:
        return time(3, 0)


def _slots(now: datetime) -> tuple[datetime | None, datetime | None]:
    """The most recent scheduled slot at or before now, and the next one after it."""
    if settings.scan_schedule not in {"daily", "weekly"}:
        return None, None
    at = _scan_time()
    today = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if settings.scan_schedule == "daily":
        previous = today if today <= now else today - timedelta(days=1)
        return previous, previous + timedelta(days=1)
    offset = (now.weekday() - int(settings.scan_schedule_day)) % 7
    previous = today - timedelta(days=offset)
    if previous > now:
        previous -= timedelta(days=7)
    return previous, previous + timedelta(days=7)


def schedule_label(now: datetime | None = None) -> str:
    # The zone is shown so a container without TZ (scans at UTC) is obvious.
    zone = (now or local_now()).tzname() or ""
    suffix = f" ({zone})" if zone else ""
    if settings.scan_schedule == "daily":
        return f"Daily at {_scan_time():%H:%M}{suffix}"
    if settings.scan_schedule == "weekly":
        return f"Weekly, {WEEKDAYS[int(settings.scan_schedule_day) % 7]} at {_scan_time():%H:%M}{suffix}"
    return "Not scheduled"


def scan_due(now: datetime, last_started: datetime | None) -> datetime | None:
    """The slot to run now, if one is due and not too late; else None."""
    previous, _ = _slots(now)
    if previous is None or now - previous > LATE_LIMIT:
        return None
    if last_started is not None and last_started >= previous:
        return None
    return previous


def run_due_tasks(now: datetime | None = None) -> str | None:
    """Start the scheduled scan if it is due. Returns the scan id when one starts.

    The slot is marked done only once the scan has started, so a start that
    fails (Bindery's database unreadable, say) is retried until LATE_LIMIT."""
    from .scanner import scan_is_running, start_scan

    now = now or local_now()
    slot = scan_due(now, _last_started("library_scan"))
    if slot is None or scan_is_running():
        return None
    scan_id = start_scan()
    if scan_id:
        _set_last_started("library_scan", now)
    return scan_id


DUPLICATE_FIX_EVERY = timedelta(hours=1)


def run_duplicate_fix(now: datetime | None = None) -> bool:
    """Fix proven duplicate Bindery entries once an hour, when it is on and actions are on."""
    from .duplicates import start_fix

    if not (settings.fix_duplicates and settings.allow_actions):
        return False
    now = now or local_now()
    last = _last_started("duplicate_fix")
    if last is not None and now - last < DUPLICATE_FIX_EVERY:
        return False
    if start_fix(auto=True):
        _set_last_started("duplicate_fix", now)
        return True
    return False


def _parse(value: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.failure: dict[str, str] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bookguard-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(30)

    def tick(self, now: datetime | None = None) -> None:
        try:
            if run_due_tasks(now):
                self.failure = None
        except Exception as exc:  # noqa: BLE001 - shown in Health and on the task
            self.failure = {"at": datetime.now(timezone.utc).isoformat(), "error": str(exc)[:500] or type(exc).__name__}
        try:
            run_duplicate_fix(now)
        except Exception:  # noqa: BLE001 - its own status shows on the Duplicates page
            pass

    def last_failure(self) -> dict[str, str] | None:
        """Why the scheduled scan last failed to start, until a scan starts after it.

        Kept after the retry window ends, so a skipped night stays visible."""
        failure = self.failure
        if not failure:
            return None
        started = _parse(str((latest_scan() or {}).get("started_at") or ""))
        failed = _parse(failure["at"])
        if started and failed and started > failed:
            return None
        return failure


scheduler = Scheduler()


def _duration(start: str | None, end: str | None) -> str:
    try:
        seconds = int((datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).total_seconds())
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def _last_observe() -> str:
    with local_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_observations'"
        ).fetchone()
        row = conn.execute("SELECT MAX(last_seen_at) AS at FROM automation_observations").fetchone() if exists else None
    return str(row["at"] or "") if row else ""


def scheduled_tasks(now: datetime | None = None) -> list[dict[str, Any]]:
    """Every recurring job, for the System page."""
    from .config import ConfigurationError, load_automation_settings
    from .import_watch import watcher
    from .scanner import scan_is_running

    now = now or local_now()
    scan = latest_scan()
    running = scan_is_running()
    _, next_slot = _slots(now)
    scan_last = ""
    if scan:
        status = str(scan.get("status") or "")
        took = _duration(scan.get("started_at"), scan.get("finished_at"))
        scan_last = "Running now" if running else f"{status.capitalize()}" + (f" · {took}" if took else "")
    failure = scheduler.last_failure()
    if failure and not running:
        scan_last = f"Scheduled scan couldn't start: {failure['error']}"

    imports = watcher.status()
    imports_next = ""
    if imports["enabled"] and imports["lastCheckAt"]:
        imports_next = (datetime.fromisoformat(imports["lastCheckAt"]) + timedelta(minutes=imports["minutes"])).isoformat()

    try:
        mode = load_automation_settings().automation_mode
    except ConfigurationError:
        mode = "invalid"

    duplicate_last = _last_started("duplicate_fix")
    return [
        {
            "key": "library_scan",
            "name": "Scan library",
            "about": "Compares every file with what Bindery expects",
            "schedule": schedule_label(now),
            "lastAt": str((scan or {}).get("finished_at") or (scan or {}).get("started_at") or ""),
            "last": scan_last,
            "nextAt": next_slot.isoformat() if next_slot else "",
            "running": running,
            "action": "scan",
        },
        {
            "key": "import_check",
            "name": "Check new imports",
            "about": "Scans and verifies files Bindery imported since the last look",
            "schedule": f"Every {imports['minutes']} min" if imports["enabled"] else "Off",
            "lastAt": str(imports["lastCheckAt"] or ""),
            "last": (f"{imports['lastChecked']} new" if imports["lastState"] == "checked"
                     else "Watching from now" if imports["lastState"] == "started"
                     else str(imports["lastState"] or "").replace("_", " ")),
            "nextAt": imports_next,
            "running": False,
            "action": "imports" if imports["enabled"] else "",
        },
        {
            "key": "duplicate_fix",
            "name": "Fix duplicate entries",
            "about": "Hides proven duplicate Bindery entries and joins split ebook/audiobook entries",
            "schedule": "Every hour" if settings.fix_duplicates and settings.allow_actions
            else "Off (turn on actions and duplicate fixing in Settings)",
            "lastAt": duplicate_last.isoformat() if duplicate_last else "",
            "last": "",
            "nextAt": "",
            "running": False,
            "action": "",
        },
        {
            "key": "observe_check",
            "name": "Observe check",
            "about": "Decides what automation would do, without changing anything",
            "schedule": "By hand" if mode == "observe" else "Off (Observe mode is not on)",
            "lastAt": _last_observe(),
            "last": "",
            "nextAt": "",
            "running": False,
            "action": "observe" if mode == "observe" else "",
        },
    ]


