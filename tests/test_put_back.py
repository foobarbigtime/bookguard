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
    scans = 0

    def __init__(self, *args, **kwargs):
        pass

    def scan_library(self):
        FakeBindery.scans += 1


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
    FakeBindery.scans = 0
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
        sha256=hashlib.sha256(BYTES).hexdigest(), size_bytes=len(BYTES),
    )
    finish_cleanup_action(cleanup_id, "applied")
    return {"id": cleanup_id, "original": original, "moved": moved}


def test_put_back_moves_the_file_home_and_asks_bindery_to_scan(quarantined):
    put_back_id, message = put_back_module.put_back(quarantined["id"])

    assert quarantined["original"].read_bytes() == BYTES
    assert not quarantined["moved"].exists()
    assert FakeBindery.scans == 1
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
    assert FakeBindery.scans == 0


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
