"""Replace: quarantine, then Bindery blocklists the release and fetches a new copy."""

from __future__ import annotations

import pytest

from app import replace
from app.activity import activity_events
from app.actions import ActionError
from app.bindery_client import BinderyClientError
from app.config import settings
from app.db import (
    add_result,
    cleanup_action_by_id,
    create_cleanup_action,
    create_scan,
    finish_cleanup_action,
    finish_scan,
    init_local_db,
    local_conn,
    result_by_id,
)

HISTORY = {"items": [
    {"id": 54, "eventType": "bookImported", "sourceTitle": "Mary.Mary.epub"},
    {"id": 55, "eventType": "grabbed", "sourceTitle": "Mary.Mary.epub"},
]}


class FakeBindery:
    calls: list = []
    history: dict = HISTORY
    search_answer: dict = {"results": {"3": {"ok": True}}}

    def __init__(self, *args, **kwargs):
        pass

    def list_history(self, book_id, limit=100):
        if FakeBindery.history is None:
            raise BinderyClientError("Bindery is down")
        return FakeBindery.history

    def blocklist_history(self, history_id):
        FakeBindery.calls.append(("blocklist", history_id))

    def set_book_monitored(self, book_id, monitored):
        FakeBindery.calls.append(("monitored", book_id, monitored))

    def search_book_automatic(self, book_id):
        FakeBindery.calls.append(("search", book_id))
        return FakeBindery.search_answer


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    init_local_db()
    create_scan("scan-1", 1)
    add_result("scan-1", {
        "file_id": 7, "book_id": 3, "author": "Author", "title": "Mary, Mary", "format": "ebook",
        "stored_path": "/data/books/Author/Mary.epub", "local_path": "/books/Author/Mary.epub",
        "classification": "REJECT", "risk_score": 90, "reason_code": "MISMATCH", "reasons": [], "metadata": {},
    })
    finish_scan("scan-1")
    with local_conn() as conn:
        result = result_by_id(conn.execute("SELECT id FROM scan_results").fetchone()["id"])
    quarantined = []

    def fake_quarantine(item):
        quarantined.append(item["id"])
        FakeBindery.calls.append(("quarantine",))
        cleanup_id = create_cleanup_action(item, "TRIAGE_QUARANTINE")
        finish_cleanup_action(cleanup_id, "applied")
        return cleanup_id, "/quarantine/Mary.epub"

    FakeBindery.calls, FakeBindery.history = [], HISTORY
    FakeBindery.search_answer = {"results": {"3": {"ok": True}}}
    monkeypatch.setattr(replace, "BinderyClient", FakeBindery)
    monkeypatch.setattr(replace, "resolve_bindery_api_key", lambda: "key")
    monkeypatch.setattr(replace, "triage_quarantine", fake_quarantine)
    FakeBindery.files = [{"stored_path": "/data/books/Author/Mary.epub"}]  # only this file
    monkeypatch.setattr(replace, "bindery_files_for_book", lambda book_id, fmt: FakeBindery.files)
    return {"result": result, "quarantined": quarantined}


def _event(cleanup_id):
    return next(e for e in activity_events() if e["key"] == f"cleanup-{cleanup_id}")


def test_replace_quarantines_then_bindery_blocklists_and_searches(book):
    cleanup_id, message = replace.triage_replace(book["result"])

    assert FakeBindery.calls == [("quarantine",), ("blocklist", 55), ("monitored", 3, True), ("search", 3)]
    assert message == "Replacement requested: Bindery is searching for a new copy and will import it."
    assert cleanup_action_by_id(cleanup_id)["status"] == "applied"  # Put back stays possible
    event = _event(cleanup_id)
    assert event["sentence"] == "You replaced “Mary, Mary”"
    assert event["resultLabel"] == "Done"


def test_replace_when_bindery_refuses_the_search_asks_for_help(book):
    FakeBindery.search_answer = {"results": {"3": {"ok": False, "code": "auto_grab_disabled",
                                                   "error": "automatic grabbing is disabled"}}}
    cleanup_id, message = replace.triage_replace(book["result"])

    assert message.startswith("Needs you: the file is in quarantine")
    assert "automatic grabbing is disabled" in message
    event = _event(cleanup_id)
    assert event["resultLabel"] == "Waiting for you"
    assert event["sentence"] == "You quarantined “Mary, Mary”; Bindery needs you to start the replacement"
    assert "Automatic search" in event["detail"]
    assert "Note:" not in message  # the old release was blocklisted, nothing to report


def test_refused_search_also_reports_when_nothing_was_blocklisted(book):
    FakeBindery.history = {"items": []}
    FakeBindery.search_answer = {"results": {"3": {"ok": False, "code": "auto_grab_disabled"}}}
    _, message = replace.triage_replace(book["result"])

    assert message.startswith("Needs you") and "nothing was blocklisted" in message


def test_replace_refuses_when_bindery_has_another_copy(book):
    FakeBindery.files = [
        {"stored_path": "/data/books/Author/Mary.epub"},
        {"stored_path": "/data/books/Author/Mary (2).epub"},
    ]
    with pytest.raises(ActionError, match="already has another copy"):
        replace.triage_replace(book["result"])
    assert book["quarantined"] == [] and FakeBindery.calls == []


def test_waiting_entry_clears_once_bindery_has_the_book_again(book, monkeypatch):
    from app import history

    FakeBindery.search_answer = {"results": {"3": {"ok": False, "code": "auto_grab_disabled"}}}
    cleanup_id, _ = replace.triage_replace(book["result"])
    assert _event(cleanup_id)["resultLabel"] == "Waiting for you"

    monkeypatch.setattr(history, "bindery_files_for_book", lambda book_id, fmt: [{"stored_path": "/x"}])
    event = _event(cleanup_id)
    assert (event["resultLabel"], event["sentence"]) == ("Done", "You replaced “Mary, Mary”")


def test_replace_without_download_history_still_searches_and_says_so(book):
    FakeBindery.history = {"items": []}
    _, message = replace.triage_replace(book["result"])

    assert ("search", 3) in FakeBindery.calls and not any(c[0] == "blocklist" for c in FakeBindery.calls)
    assert "nothing was blocklisted" in message


def test_replace_moves_nothing_when_bindery_is_unreachable(book):
    FakeBindery.history = None
    with pytest.raises(ActionError, match="nothing was moved"):
        replace.triage_replace(book["result"])
    assert book["quarantined"] == []


def test_flagged_import_clears_once_bindery_no_longer_tracks_that_path(book, monkeypatch):
    from app import activity
    from app.import_watch import init_import_watch_db

    init_import_watch_db()
    with local_conn() as conn:
        conn.execute(
            """INSERT INTO import_checks(file_id, book_id, format, title, author, stored_path, result_id,
                   classification, verdict, error, trigger, checked_at)
               VALUES (1, 9, 'ebook', 'Harry Potter', 'J.K. Rowling', '/data/books/old/hp.epub', NULL,
                   'REJECT', 'UNSAFE_FILE', '', 'watch', '2026-10-03T20:02:00+00:00')"""
        )
        conn.commit()
    tracked = [{"book_id": 9}]
    monkeypatch.setattr(activity, "associations_inside_path", lambda path: tracked)

    def imported():
        return next(e for e in activity_events() if e["kind"] == "import_check")

    assert imported()["resultLabel"] == "Waiting for you"
    tracked.clear()  # the user fixed it in Bindery: that path is no longer tracked
    event = imported()
    assert event["resultLabel"] == "Done"
    assert event["sentence"].endswith("it failed the safety checks, and it has since been fixed or removed")
