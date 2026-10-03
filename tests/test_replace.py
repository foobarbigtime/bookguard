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
    return {"result": result, "quarantined": quarantined}


def _event(cleanup_id):
    return next(e for e in activity_events() if e["key"] == f"cleanup-{cleanup_id}")


def test_replace_quarantines_then_bindery_blocklists_and_searches(book):
    cleanup_id, message = replace.triage_replace(book["result"])

    assert FakeBindery.calls == [("quarantine",), ("blocklist", 55), ("monitored", 3, True), ("search", 3)]
    assert message == "Replacement requested: Bindery is searching for a new copy and will import it."
    assert cleanup_action_by_id(cleanup_id)["status"] == "applied"  # Put back stays possible
    event = _event(cleanup_id)
    assert event["sentence"] == "You replaced “Mary, Mary”; Bindery is getting a new copy"
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
