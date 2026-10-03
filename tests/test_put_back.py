"""Put back: undo one Triage quarantine, refusing anything unsafe."""

from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import put_back as put_back_module
from app.activity import activity_events
from app.actions import ActionError
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
    record_cleanup_location,
    result_by_id,
)
from app.routes.pages import router as pages_router
from app.routes.triage import router as triage_router

BYTES = b"the real book"


class FakeBindery:
    """Bindery's API as Put back uses it; `tracked` stands in for its book_files table."""

    scans = 0
    tracked: list = []
    unmatched: list = []
    adopted: list = []
    monitored: list = []
    register_on_scan = True
    scan_book = 3
    reassigned: list = []
    preview = {"status": "move", "destination": "/data/books/Author/Mary/Mary.epub"}

    def __init__(self, *args, **kwargs):
        pass

    def scan_library(self):
        FakeBindery.scans += 1
        if FakeBindery.register_on_scan:
            title = "Mary, Mary" if FakeBindery.scan_book == 3 else "Mary"
            FakeBindery.tracked.append({"book_id": FakeBindery.scan_book, "title": title, "format": "ebook"})

    def preview_manual_reassignment(self, path, book_id, *, file_format="ebook"):
        return FakeBindery.preview

    def reassign_manual_import(self, path, book_id, *, file_format="ebook"):
        FakeBindery.reassigned.append((path, book_id, file_format))
        FakeBindery.tracked[:] = [{"book_id": book_id, "title": "Mary, Mary", "format": file_format}]

    def list_unmatched(self, *, search="", file_format=None):
        ran = "after" if FakeBindery.scans else "before"
        return {"items": FakeBindery.unmatched, "scan": {"running": False, "ranAt": ran}}

    def adopt_unmatched(self, row_id, book_id):
        FakeBindery.adopted.append((row_id, book_id))
        FakeBindery.tracked.append({"book_id": book_id, "title": "Mary, Mary", "format": "ebook"})

    def set_book_monitored(self, book_id, monitored):
        FakeBindery.monitored.append((book_id, monitored))


@pytest.fixture
def quarantined(tmp_path, monkeypatch):
    books, quarantine = tmp_path / "books", tmp_path / "quarantine"
    (books / "Author").mkdir(parents=True)
    quarantine.mkdir()
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "ebook_root", str(books))
    monkeypatch.setattr(settings, "audiobook_root", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(settings, "quarantine_root", str(quarantine))
    monkeypatch.setattr(settings, "allow_actions", True)
    monkeypatch.setattr(put_back_module, "BinderyClient", FakeBindery)
    monkeypatch.setattr(put_back_module, "resolve_bindery_api_key", lambda: "key")
    monkeypatch.setattr(settings, "ebook_bindery_prefix", "/data/books")
    FakeBindery.scans, FakeBindery.register_on_scan, FakeBindery.scan_book = 0, True, 3
    FakeBindery.tracked, FakeBindery.unmatched, FakeBindery.adopted, FakeBindery.monitored = [], [], [], []
    FakeBindery.reassigned = []
    FakeBindery.preview = {"status": "move", "destination": "/data/books/Author/Mary/Mary.epub"}
    monkeypatch.setattr(put_back_module, "associations_inside_path", lambda path: list(FakeBindery.tracked))
    monkeypatch.setattr(
        put_back_module, "bindery_files_for_book",
        lambda book_id, fmt: [t for t in FakeBindery.tracked if t["book_id"] == book_id and t["format"] == fmt],
    )
    monkeypatch.setattr(put_back_module, "FOLLOWUP_POLL_SECONDS", 0)
    monkeypatch.setattr(put_back_module, "_start_followup", lambda put_back_id, row: None)
    init_local_db()
    create_scan("scan-1", 1)
    add_result("scan-1", {
        "file_id": 7, "book_id": 3, "author": "Author", "title": "Mary, Mary", "format": "ebook",
        "stored_path": "/data/books/Author/Mary.epub", "local_path": str(books / "Author" / "Mary.epub"),
        "classification": "REJECT", "risk_score": 90, "reason_code": "MISMATCH", "reasons": [], "metadata": {},
    })
    finish_scan("scan-1")
    with local_conn() as conn:
        result_id = conn.execute("SELECT id FROM scan_results").fetchone()["id"]
    original, moved = books / "Author" / "Mary.epub", quarantine / "Mary.epub"
    moved.write_bytes(BYTES)
    cleanup_id = create_cleanup_action(result_by_id(result_id), "TRIAGE_QUARANTINE")
    record_cleanup_location(
        cleanup_id, original_path=str(original), quarantine_path=str(moved),
        sha256=hashlib.sha256(BYTES).hexdigest(), size_bytes=len(BYTES), was_monitored=True,
    )
    finish_cleanup_action(cleanup_id, "applied")
    return {"id": cleanup_id, "original": original, "moved": moved}


def test_put_back_moves_the_file_home(quarantined):
    put_back_id, message = put_back_module.put_back(quarantined["id"])

    assert quarantined["original"].read_bytes() == BYTES
    assert not quarantined["moved"].exists()
    assert "Put back" in message
    row = cleanup_action_by_id(put_back_id)
    assert (row["action_kind"], row["status"], row["put_back_of"]) == ("PUT_BACK", "applied", quarantined["id"])
    with pytest.raises(ActionError, match="already put back"):
        put_back_module.put_back(quarantined["id"])


def test_put_back_refuses_when_the_original_path_is_taken(quarantined):
    quarantined["original"].write_bytes(b"something new")

    with pytest.raises(ActionError, match="already at the original path"):
        put_back_module.put_back(quarantined["id"])
    assert quarantined["original"].read_bytes() == b"something new"
    assert quarantined["moved"].read_bytes() == BYTES


def test_put_back_refuses_a_missing_or_changed_file(quarantined):
    quarantined["moved"].write_bytes(b"the fake book")  # same size, other bytes
    with pytest.raises(ActionError, match="changed since"):
        put_back_module.put_back(quarantined["id"])
    quarantined["moved"].write_bytes(b"longer than before")
    with pytest.raises(ActionError, match="changed size"):
        put_back_module.put_back(quarantined["id"])
    quarantined["moved"].unlink()
    with pytest.raises(ActionError, match="missing"):
        put_back_module.put_back(quarantined["id"])
    assert not quarantined["original"].exists()


def test_put_back_refuses_a_destination_outside_the_library(quarantined, tmp_path):
    with local_conn() as conn:
        conn.execute("UPDATE cleanup_actions SET original_path=? WHERE id=?",
                     (str(tmp_path / "Mary.epub"), quarantined["id"]))
        conn.commit()
    with pytest.raises(ActionError, match="outside the library"):
        put_back_module.put_back(quarantined["id"])
    assert quarantined["moved"].exists()


def test_put_back_shows_in_activity_and_on_the_detail_page(quarantined):
    app = FastAPI()
    app.include_router(pages_router)
    app.include_router(triage_router)
    client = TestClient(app)

    page = client.get(f"/activity/cleanup/{quarantined['id']}")
    assert f'data-put-back="{quarantined["id"]}"' in page.text
    assert client.post(f"/api/quarantine/{quarantined['id']}/put-back", json={"confirm": "x"}).status_code == 400

    response = client.post(f"/api/quarantine/{quarantined['id']}/put-back", json={"confirm": "PUT_BACK"})
    assert response.status_code == 200, response.text
    event = next(e for e in activity_events() if e["key"] == f"cleanup-{response.json()['cleanup_id']}")
    assert event["sentence"] == "You put back “Mary, Mary”"
    assert (event["whoLabel"], event["resultLabel"]) == ("You", "Done")

    again = client.post(f"/api/quarantine/{quarantined['id']}/put-back", json={"confirm": "PUT_BACK"})
    assert again.status_code == 409
    assert "data-put-back" not in client.get(f"/activity/cleanup/{quarantined['id']}").text


def test_put_back_refuses_when_bindery_already_has_another_copy(quarantined, monkeypatch):
    monkeypatch.setattr(put_back_module, "bindery_files_for_book", lambda book_id, fmt: [{"file_id": 99}])
    with pytest.raises(ActionError, match="already has another copy"):
        put_back_module.put_back(quarantined["id"])
    assert quarantined["moved"].exists()


def _row(cleanup_id):
    return cleanup_action_by_id(cleanup_id)


def test_followup_scan_relinks_and_monitors_again(quarantined):
    put_back_id, _ = put_back_module.put_back(quarantined["id"])

    message = put_back_module.relink_in_bindery(put_back_id, _row(quarantined["id"]))

    assert FakeBindery.scans == 1 and FakeBindery.adopted == []
    assert FakeBindery.monitored == [(3, True)]
    assert message == "Bindery is tracking the file again and monitoring the book."
    assert (_row(put_back_id)["status"], _row(put_back_id)["followup"]) == ("applied", message)


def test_followup_adopts_the_unmatched_file_to_its_own_book(quarantined):
    FakeBindery.register_on_scan = False
    FakeBindery.unmatched = [
        {"id": 41, "rootPath": "/data/books", "relPath": "Other/Else.epub", "members": ["Else.epub"]},
        {"id": 42, "rootPath": "/data/books", "relPath": "Author/Mary.epub", "members": ["Mary.epub"]},
    ]
    put_back_id, _ = put_back_module.put_back(quarantined["id"])

    put_back_module.relink_in_bindery(put_back_id, _row(quarantined["id"]))

    assert FakeBindery.adopted == [(42, 3)]
    assert FakeBindery.monitored == [(3, True)]


def test_followup_that_cannot_relink_asks_for_help_and_leaves_monitoring_off(quarantined):
    FakeBindery.register_on_scan = False
    put_back_id, _ = put_back_module.put_back(quarantined["id"])

    message = put_back_module.relink_in_bindery(put_back_id, _row(quarantined["id"]))

    assert message.startswith("Needs you")
    assert FakeBindery.monitored == []
    assert _row(put_back_id)["status"] == "attention"
    event = next(e for e in activity_events() if e["key"] == f"cleanup-{put_back_id}")
    assert event["resultLabel"] == "Waiting for you"
    assert event["sentence"] == "You put back “Mary, Mary”; Bindery needs you to finish"
    with pytest.raises(ActionError, match="already put back"):
        put_back_module.put_back(quarantined["id"])


@pytest.fixture
def triage_env(quarantined, monkeypatch, tmp_path):
    from app import triage

    calls = []

    class Bindery:
        def __init__(self, *args, **kwargs):
            pass

        def get_book(self, book_id):
            return {"id": book_id, "monitored": True}

        def set_book_monitored(self, book_id, monitored):
            calls.append(("monitored", book_id, monitored))

    moved = tmp_path / "quarantine" / "Mary-2.epub"

    def fake_quarantine(book_id, stored_path, local_path):
        calls.append(("moved", book_id))
        moved.write_bytes(BYTES)
        return str(moved), quarantined["original"]

    monkeypatch.setattr(triage, "BinderyClient", Bindery)
    monkeypatch.setattr(triage, "resolve_bindery_api_key", lambda: "key")
    monkeypatch.setattr(triage, "triage_action_preview", lambda result, action: {"safe": True})
    monkeypatch.setattr(triage, "mount_is_writable", lambda path: True)
    monkeypatch.setattr(triage, "_wait_for_bindery_row_gone", lambda file_id: None)
    monkeypatch.setattr(triage, "quarantine_file", fake_quarantine)
    with local_conn() as conn:
        result_id = conn.execute("SELECT id FROM scan_results").fetchone()["id"]
    return {"triage": triage, "calls": calls, "result": result_by_id(result_id)}


def test_quarantine_stops_bindery_monitoring_first_and_remembers_it(triage_env):
    cleanup_id, _ = triage_env["triage"].triage_quarantine(triage_env["result"])

    assert triage_env["calls"] == [("monitored", 3, False), ("moved", 3)]
    assert cleanup_action_by_id(cleanup_id)["was_monitored"] == 1


def test_quarantine_that_fails_turns_monitoring_back_on(triage_env, monkeypatch):
    def broken(*args):
        raise ActionError("disk full")

    monkeypatch.setattr(triage_env["triage"], "quarantine_file", broken)
    with pytest.raises(ActionError, match="disk full"):
        triage_env["triage"].triage_quarantine(triage_env["result"])
    assert triage_env["calls"] == [("monitored", 3, False), ("monitored", 3, True)]


def test_followup_moves_the_file_off_a_wrong_match_with_fix_match(quarantined):
    FakeBindery.scan_book = 9  # Bindery's scan picks "Mary", the wrong book
    put_back_id, _ = put_back_module.put_back(quarantined["id"])

    message = put_back_module.relink_in_bindery(put_back_id, _row(quarantined["id"]))

    assert FakeBindery.reassigned == [("/data/books/Author/Mary.epub", 3, "ebook")]
    assert FakeBindery.monitored == [(3, True)]
    assert message == "Bindery is tracking the file again and monitoring the book."


def test_wrong_match_that_bindery_will_not_move_safely_names_the_book_and_the_button(quarantined):
    FakeBindery.scan_book = 9
    FakeBindery.preview = {"status": "move", "destination": "/elsewhere/Mary.epub"}
    put_back_id, _ = put_back_module.put_back(quarantined["id"])

    message = put_back_module.relink_in_bindery(put_back_id, _row(quarantined["id"]))

    assert FakeBindery.reassigned == [] and FakeBindery.monitored == []
    assert message.startswith("Needs you: Bindery's scan put the file on “Mary”.")
    assert "More → Fix match" in message
    assert _row(put_back_id)["status"] == "attention"
