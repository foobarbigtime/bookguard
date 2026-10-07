"""System: health checks with how-to-fix, the scan schedule, and the page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import health, scheduler
from app.config import Settings, settings
from app.db import create_scan, init_local_db
from app.routes.pages import router as pages_router
from app.routes.system import router as system_router

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def system(tmp_path, monkeypatch):
    bindery = tmp_path / "bindery.db"
    with sqlite3.connect(bindery) as conn:
        conn.execute("CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT, path TEXT)")
    for folder in ("books", "audiobooks"):
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "bindery_db", str(bindery))
    monkeypatch.setattr(settings, "ebook_root", str(tmp_path / "books"))
    monkeypatch.setattr(settings, "audiobook_root", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(settings, "scan_schedule", "off")
    monkeypatch.setattr(health, "_uid", lambda: 99)
    monkeypatch.setattr(scheduler.scheduler, "failure", None)
    init_local_db()
    return tmp_path


def scanner_up(version="ClamAV 1.5.4/28141/Fri Oct  2 06:26:12 2026"):
    return {"enabled": True, "configured": True, "reachable": True, "version": version, "message": ""}


def keys(result):
    return {check["key"]: check["level"] for check in result["problems"]}


def test_all_clear_when_everything_works(system):
    result = health.health_checks(scanner_up(), now=NOW, scan_running=lambda: False)
    assert result["level"] == "ok" and result["problems"] == []
    assert "Virus definitions current (Oct 02)" in [c["title"] for c in result["passing"]]


def test_old_virus_definitions_and_a_silent_scanner_are_reported(system):
    old = health.health_checks(scanner_up(), now=NOW + timedelta(days=5), scan_running=lambda: False)
    assert keys(old) == {"virus_definitions": "warn"}
    assert "freshclam" in old["problems"][0]["fix"]
    down = {"enabled": True, "configured": True, "reachable": False, "message": "Connection refused."}
    assert keys(health.health_checks(down, now=NOW, scan_running=lambda: False)) == {"virus_scanner": "error"}


def test_missing_bindery_database_and_library_folder_say_how_to_fix(system, monkeypatch):
    monkeypatch.setattr(settings, "bindery_db", str(system / "absent.db"))
    monkeypatch.setattr(settings, "ebook_root", str(system / "nowhere"))
    result = health.health_checks(scanner_up(), now=NOW, scan_running=lambda: False)
    assert keys(result) == {"bindery_db": "error", "ebook_root": "error"}
    assert all(check["fix"] for check in result["problems"])
    assert result["level"] == "error"


def test_a_scan_left_running_by_a_restart_is_reported(system):
    create_scan("cut-short", 100)
    result = health.health_checks(scanner_up(), now=NOW, scan_running=lambda: False)
    assert keys(result) == {"interrupted_scan": "warn"}
    assert health.health_checks(scanner_up(), now=NOW, scan_running=lambda: True)["problems"] == []


def start(day, hour, minute=5, revision="aaa", previous="aaa"):
    at = datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).isoformat()
    return {"started_at": at, "revision": revision, "previous_revision": previous}


def test_nightly_restarts_are_spotted_but_updates_are_not():
    nightly = [start(28, 0), start(29, 0, 10), start(30, 23, 58), start(1, 0, 2)]
    assert health.nightly_restarts(nightly, NOW) == {"hour": 0, "nights": 4}
    updates = [start(28, 0, revision="b", previous="a"), start(29, 0, revision="c", previous="b"),
               start(30, 0, revision="d", previous="c")]
    assert health.nightly_restarts(updates, NOW) is None
    assert health.nightly_restarts([start(29, 0), start(30, 0)], NOW) is None
    assert health.nightly_restarts([start(1, 0), start(1, 9), start(2, 17)], NOW) is None


def test_restart_hour_is_shown_in_local_time(system):
    toronto = NOW.astimezone(ZoneInfo("America/Toronto"))
    midnight_local = [start(day, 4) for day in (28, 29, 30)] + [start(1, 4, 7)]  # 04:0x UTC = 00:0x EDT
    assert health.nightly_restarts(midnight_local, toronto) == {"hour": 0, "nights": 4}
    with health.local_conn() as conn:
        conn.execute("""CREATE TABLE app_starts (id INTEGER PRIMARY KEY, started_at TEXT, version TEXT,
                        revision TEXT, previous_version TEXT, previous_revision TEXT)""")
        conn.executemany("INSERT INTO app_starts(started_at, revision, previous_revision) VALUES (?, ?, ?)",
                         [(s["started_at"], s["revision"], s["previous_revision"]) for s in midnight_local])
        conn.commit()
    result = health.health_checks(scanner_up(), now=toronto, scan_running=lambda: False)
    assert [c["title"] for c in result["problems"]] == ["BookGuard is restarted most nights around 00:00 (EDT)"]


def test_running_as_root_is_a_warning(system, monkeypatch):
    monkeypatch.setattr(health, "_uid", lambda: 0)
    assert keys(health.health_checks(scanner_up(), now=NOW, scan_running=lambda: False)) == {"runtime_user": "warn"}


LOCAL = timezone(timedelta(hours=2))


def test_weekly_and_daily_schedules_find_the_due_slot(monkeypatch):
    monkeypatch.setattr(settings, "scan_schedule", "weekly")
    monkeypatch.setattr(settings, "scan_schedule_time", "03:00")
    monkeypatch.setattr(settings, "scan_schedule_day", 6)  # Sunday
    sunday = datetime(2026, 10, 4, 3, 30, tzinfo=LOCAL)
    assert scheduler.scan_due(sunday, None) == datetime(2026, 10, 4, 3, 0, tzinfo=LOCAL)
    assert scheduler.scan_due(sunday, sunday - timedelta(minutes=10)) is None  # already ran
    assert scheduler.scan_due(sunday + timedelta(hours=5), None) is None  # too late; wait a week
    assert scheduler.scan_due(datetime(2026, 10, 5, 3, 30, tzinfo=LOCAL), None) is None  # Monday
    assert scheduler.schedule_label(sunday) == "Weekly, Sunday at 03:00 (UTC+02:00)"

    monkeypatch.setattr(settings, "scan_schedule", "daily")
    assert scheduler.scan_due(datetime(2026, 10, 5, 3, 1, tzinfo=LOCAL), None) is not None
    monkeypatch.setattr(settings, "scan_schedule", "off")
    assert scheduler.scan_due(sunday, None) is None
    assert scheduler.schedule_label() == "Not scheduled"


def test_a_due_scan_starts_once(system, monkeypatch):
    started = []
    monkeypatch.setattr(settings, "scan_schedule", "daily")
    monkeypatch.setattr(settings, "scan_schedule_time", "03:00")
    monkeypatch.setattr("app.scanner.start_scan", lambda: started.append(1) or "scan-1")
    monkeypatch.setattr("app.scanner.scan_is_running", lambda: False)
    at = datetime(2026, 10, 4, 3, 5, tzinfo=LOCAL)
    assert scheduler.run_due_tasks(at) == "scan-1"
    assert scheduler.run_due_tasks(at + timedelta(minutes=1)) is None
    assert started == [1]


def test_a_scheduled_scan_that_fails_to_start_is_retried_and_reported(system, monkeypatch):
    monkeypatch.setattr(settings, "scan_schedule", "daily")
    monkeypatch.setattr(settings, "scan_schedule_time", "03:00")
    monkeypatch.setattr("app.scanner.scan_is_running", lambda: False)

    def broken():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("app.scanner.start_scan", broken)
    at = datetime(2026, 10, 4, 3, 5, tzinfo=LOCAL)
    scheduler.scheduler.tick(at)
    assert scheduler.scheduler.last_failure()["error"] == "unable to open database file"
    assert scheduler._last_started("library_scan") is None  # the slot is not used up

    # Still reported once the retry window has passed and nothing is due.
    scheduler.scheduler.tick(at + timedelta(hours=5))
    result = health.health_checks(scanner_up(), now=NOW, scan_running=lambda: False)
    assert keys(result) == {"scheduled_scan": "warn"}
    assert "unable to open database file" in result["problems"][0]["message"]
    scan_task = scheduler.scheduled_tasks(at)[0]
    assert scan_task["last"] == "Scheduled scan couldn't start: unable to open database file"

    # Any scan that starts afterwards clears it; a working retry does too.
    create_scan("by-hand", 1)
    assert scheduler.scheduler.last_failure() is None
    scheduler.scheduler.failure = {"at": "2999-01-01T00:00:00+00:00", "error": "x"}
    monkeypatch.setattr("app.scanner.start_scan", lambda: "scan-2")
    scheduler.scheduler.tick(at)
    assert scheduler.scheduler.failure is None and scheduler._last_started("library_scan") == at


def test_slots_follow_daylight_saving(monkeypatch):
    monkeypatch.setattr(settings, "scan_schedule", "weekly")
    monkeypatch.setattr(settings, "scan_schedule_time", "03:00")
    monkeypatch.setattr(settings, "scan_schedule_day", 6)
    toronto = ZoneInfo("America/Toronto")
    saturday = datetime(2026, 10, 31, 12, 0, tzinfo=toronto)  # EDT; clocks go back Sunday 02:00
    _, upcoming = scheduler._slots(saturday)
    assert upcoming.isoformat() == "2026-11-01T03:00:00-05:00"
    assert scheduler.scan_due(datetime(2026, 11, 1, 3, 20, tzinfo=toronto), None).isoformat() == "2026-11-01T03:00:00-05:00"
    monkeypatch.setenv("TZ", "America/Toronto")
    assert scheduler.local_zone() == toronto


def test_schedule_settings_are_validated():
    cfg = Settings()
    cfg.apply({"scan_schedule": "hourly", "scan_schedule_time": "25:99", "scan_schedule_day": 9})
    assert (cfg.scan_schedule, cfg.scan_schedule_time, cfg.scan_schedule_day) == ("off", "03:00", 6)
    cfg.apply({"scan_schedule": "Weekly", "scan_schedule_time": "4:5", "scan_schedule_day": 2})
    assert (cfg.scan_schedule, cfg.scan_schedule_time, cfg.scan_schedule_day) == ("weekly", "04:05", 2)


def test_system_page_shows_health_tasks_and_about(system, monkeypatch):
    # Keep the fixture's virus definitions current regardless of the run date.
    monkeypatch.setattr(scheduler, "local_now", lambda: NOW)
    monkeypatch.setattr("app.routes.pages._malware_report", lambda: scanner_up())
    monkeypatch.setattr(settings, "watch_imports", True)
    app = FastAPI()
    app.include_router(pages_router)
    app.include_router(system_router)
    with TestClient(app) as client:
        page = client.get("/system").text
        assert "Everything BookGuard checks is working." in page
        for task in ("Scan library", "Check new imports", "Observe check"):
            assert task in page
        assert 'data-run-task="scan"' in page and 'data-run-task="imports"' in page
        assert "Running since" in page
        assert client.get("/system/advanced").status_code == 200
        assert client.post("/api/imports/check", json={"confirm": "nope"}).status_code == 400
        assert client.post("/api/imports/check", json={"confirm": "CHECK_IMPORTS"}).status_code == 202
        assert "problems" in client.get("/api/system/health").json()


def test_virus_scanner_version_reads_short_in_about():
    assert health._scanner_text("ClamAV 1.5.4/28142/Sat Oct  3 06:24:16 2026") == "ClamAV 1.5.4 · definitions Oct 3"
    assert health._scanner_text("ClamAV 1.5.4") == "ClamAV 1.5.4"
    assert health._scanner_text("") == "Answering"


def test_first_import_check_reads_watching_from_now(system, monkeypatch):
    monkeypatch.setattr("app.import_watch.watcher.status", lambda: {
        "enabled": True, "minutes": 5, "lastCheckAt": "", "lastChecked": 0, "lastState": "started",
    })
    task = next(t for t in scheduler.scheduled_tasks() if t["key"] == "import_check")
    assert task["last"] == "Watching from now"


def test_book_actions_check_says_what_to_do_next(system, monkeypatch, tmp_path):
    from app import action_paths, config

    monkeypatch.setattr(settings, "allow_actions", False)
    check = health._book_actions()
    assert check["level"] == "ok" and "Settings → Safety and Bindery actions" in check["title"]

    monkeypatch.setattr(settings, "allow_actions", True)
    monkeypatch.setenv("BOOKGUARD_EBOOK_ACTIONS_ENABLED", "false")
    assert "BOOKGUARD_EBOOK_ACTIONS_ENABLED=true" in health._book_actions()["title"]

    monkeypatch.setenv("BOOKGUARD_EBOOK_ACTIONS_ENABLED", "true")
    monkeypatch.setenv("BOOKGUARD_EBOOK_ACTION_ROOT", str(tmp_path / "missing"))
    assert health._book_actions()["level"] == "warn"

    (tmp_path / "unrelated").mkdir()
    (tmp_path / "quarantine").mkdir()
    monkeypatch.setenv("BOOKGUARD_EBOOK_ACTION_ROOT", str(tmp_path / "unrelated"))
    monkeypatch.setattr(settings, "quarantine_root", str(tmp_path / "quarantine"))
    monkeypatch.setattr(action_paths, "mount_is_writable", lambda path: True)
    assert health._book_actions()["title"] == "The writable books folder is not your books folder"

    (tmp_path / "action").symlink_to(settings.ebook_root)  # stands in for a second mount of /books
    monkeypatch.setenv("BOOKGUARD_EBOOK_ACTION_ROOT", str(tmp_path / "action"))
    check = health._book_actions()
    assert (check["level"], check["title"]) == ("ok", "Quarantine, Replace and Put back are ready")
    assert config.load_automation_settings().ebook_actions_enabled is True
