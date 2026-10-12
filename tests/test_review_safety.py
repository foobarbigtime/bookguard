"""Bulk MISSING cleanup during library tasks, and mixed folders found only by verification."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import library_check, verifier
from app.library_review import review_item
from app.matcher import analyze_audio_identity_set, classify_audio
from app.routes import triage as routes


@pytest.fixture
def cleanup(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "latest_scan", lambda: {"id": "scan-1", "status": "complete"})
    monkeypatch.setattr(routes, "latest_results", lambda **kw: [{"id": 1, "author": "A", "title": "T"}])
    monkeypatch.setattr(routes, "missing_detach_preview", lambda row: {"safe": True})
    detached = []
    monkeypatch.setattr(routes, "detach_missing", lambda row: detached.append(row["id"]) or 41)
    saved = dict(library_check._state)
    yield TestClient(app), detached
    library_check._state.clear()
    library_check._state.update(saved)


def _detach_all(client):
    return client.post("/api/missing/detach-all", json={"confirm": "DETACH", "scan_id": "scan-1"})


def test_bulk_missing_cleanup_is_refused_while_check_library_runs(cleanup):
    client, detached = cleanup
    library_check._state.update(status="running", check_id="check-1")

    response = _detach_all(client)

    assert response.status_code == 409
    assert "Check library is running" in response.json()["detail"]
    assert detached == []


def test_bulk_missing_cleanup_says_when_no_validation_scan_started(cleanup, monkeypatch):
    client, detached = cleanup
    library_check._state.update(status="idle", check_id=None)
    monkeypatch.setattr(verifier, "verification_job_status", lambda: {"status": "running"})

    body = _detach_all(client).json()

    assert detached == [1]
    assert body["scan_id"] is None
    assert "was started" not in body["message"]
    assert "could not start" in body["message"]


def test_bulk_missing_cleanup_reports_a_started_validation_scan(cleanup, monkeypatch):
    client, _ = cleanup
    library_check._state.update(status="idle", check_id=None)
    monkeypatch.setattr(routes, "start_scan", lambda: "scan-2")

    body = _detach_all(client).json()

    assert body["scan_id"] == "scan-2"
    assert "A validation scan was started." in body["message"]


def test_music_tagged_mixed_folder_is_flagged_mixed_from_verification_evidence(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "reject_music_mismatch", True)
    probes = [
        {"album": "Lights Out", "artist": "Narrator X", "genre": "Pop"},
        {"album": "The Road North", "artist": "Other Y", "genre": "Pop"},
        {"album": "Cold Harbour", "artist": "Other Z", "genre": "Pop"},
    ]
    assert classify_audio("Lights Out", "James Patterson", probes)[2] == "MUSIC_MISMATCH"
    assert analyze_audio_identity_set("Lights Out", "James Patterson", probes)["mixedContent"]

    row = {"id": 9, "book_id": 1, "title": "Lights Out", "author": "James Patterson", "format": "audiobook",
           "classification": "REJECT", "reason_code": "MUSIC_MISMATCH", "risk_score": 100,
           "stored_path": "/audiobooks/x", "reasons": [], "metadata": {}}
    verification = {"id": 3, "verdict": "WRONG_CONTENT", "confidence": 98,
                    "evidence": {"reasonCode": "MIXED_AUDIO_CONTENT"}}

    item = review_item(row, verification)

    assert item["mixedAudio"] is True
    assert item["suggestion"].startswith("This folder contains multiple works.")
