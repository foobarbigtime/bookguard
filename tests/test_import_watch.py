"""Checking each new Bindery import: only new files, once, never during a scan."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import import_watch, scanner, verifier
from app.activity import activity_events
from app.config import settings
from app.db import add_result, create_scan, finish_scan, init_local_db, local_conn
from app.import_watch import ImportWatcher, check_new_imports, init_import_watch_db
from app.library_review import open_review_items
from app.routes.system import router as system_router


@pytest.fixture
def world(tmp_path, monkeypatch):
    bindery = tmp_path / "bindery.db"
    with sqlite3.connect(bindery) as conn:
        conn.executescript(
            """
            CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, status TEXT, author_id INTEGER);
            CREATE TABLE book_files(id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER, format TEXT, path TEXT);
            INSERT INTO authors VALUES (1, 'John Grisham');
            INSERT INTO books VALUES (10, 'The Firm', 'imported', 1), (11, 'Broker', 'imported', 1);
            INSERT INTO book_files(book_id, format, path) VALUES (10, 'ebook', '/data/media/books/The Firm.epub');
            """
        )
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "bindery_db", str(bindery))
    monkeypatch.setattr(settings, "watch_imports", True)
    monkeypatch.setattr(settings, "verification_enabled", True)
    init_local_db()
    init_import_watch_db()
    create_scan("full", 1)
    add_result("full", {
        "file_id": 1, "book_id": 10, "author": "John Grisham", "title": "The Firm", "format": "ebook",
        "stored_path": "/data/media/books/The Firm.epub", "local_path": "/books/The Firm.epub",
        "classification": "PASS", "risk_score": 0, "reason_code": "MATCH", "reasons": [], "metadata": {},
    })
    finish_scan("full")

    scanned = []

    def fake_scan(row, audiobook_progress=None):
        scanned.append(row["file_id"])
        return {**row, "local_path": "/books/x.epub", "classification": "REJECT", "risk_score": 90,
                "reason_code": "MISMATCH", "reasons": ["Title differs."], "metadata": {}}

    monkeypatch.setattr(scanner, "_scan_one", fake_scan)
    monkeypatch.setattr(scanner, "scan_is_running", lambda: False)
    monkeypatch.setattr(verifier, "verify_result", lambda result: {"verdict": "WRONG_CONTENT"})
    return {"bindery": bindery, "scanned": scanned}


def import_file(world, book_id=11, path="/data/media/books/Broker.epub"):
    with sqlite3.connect(world["bindery"]) as conn:
        conn.execute("INSERT INTO book_files(book_id, format, path) VALUES (?, 'ebook', ?)", (book_id, path))


def test_first_run_starts_from_now_without_rechecking_the_library(world):
    assert check_new_imports() == {"state": "started", "checked": 0}
    assert check_new_imports() == {"state": "checked", "checked": 0}
    assert world["scanned"] == []


def test_a_new_import_is_scanned_verified_and_shown_in_review_and_activity(world):
    check_new_imports()
    import_file(world)
    assert check_new_imports("webhook") == {"state": "checked", "checked": 1}
    assert check_new_imports() == {"state": "checked", "checked": 0}  # once only
    assert world["scanned"] == [2]

    item = [i for i in open_review_items() if i["title"] == "Broker"][0]
    assert item["classification"] == "REJECT"
    event = [e for e in activity_events() if e["kind"] == "import_check"][0]
    assert event["sentence"] == "Bindery imported “Broker”; it contains the wrong file"
    assert event["result"] == "waiting"
    assert event["detailHref"] == f"/review#book-{item['id']}"


def test_waits_while_a_full_scan_runs(world, monkeypatch):
    check_new_imports()
    import_file(world)
    monkeypatch.setattr(scanner, "scan_is_running", lambda: True)
    assert check_new_imports()["state"] == "waiting_for_scan"
    monkeypatch.setattr(scanner, "scan_is_running", lambda: False)
    assert check_new_imports()["checked"] == 1


def test_a_failed_check_is_recorded_and_not_retried_forever(world, monkeypatch):
    check_new_imports()
    import_file(world)

    def broken(row, audiobook_progress=None):
        raise FileNotFoundError("missing on disk")

    monkeypatch.setattr(scanner, "_scan_one", broken)
    assert check_new_imports()["checked"] == 1
    assert check_new_imports()["checked"] == 0
    event = [e for e in activity_events() if e["kind"] == "import_check"][0]
    assert (event["result"], event["detail"]) == ("failed", "missing on disk")


def test_turned_off_does_nothing(world, monkeypatch):
    monkeypatch.setattr(settings, "watch_imports", False)
    assert check_new_imports() == {"state": "off", "checked": 0}


def test_unreadable_bindery_database_is_reported_not_raised(world, monkeypatch, tmp_path):
    check_new_imports()
    monkeypatch.setattr(settings, "bindery_db", str(tmp_path / "absent.db"))
    status = ImportWatcher()
    outcome = status.run_once("timer")
    assert outcome["state"] == "error"
    assert status.status()["lastError"]


def test_webhook_wakes_the_watcher_only_for_imports(world, monkeypatch):
    nudges = []
    monkeypatch.setattr(import_watch.watcher, "nudge", lambda: nudges.append(1))
    app = FastAPI()
    app.include_router(system_router)
    with TestClient(app) as client:
        imported = client.post("/api/bindery/webhook", json={"eventType": "bookImported", "title": "Broker"})
        assert imported.status_code == 202 and imported.json()["checking"] is True
        assert client.post("/api/bindery/webhook", json={"eventType": "test"}).json()["checking"] is False
        assert client.post("/api/bindery/webhook", content=b"not json",
                           headers={"Content-Type": "application/json"}).status_code == 202
    assert nudges == [1]
    with local_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_checks").fetchone()[0] == 0
