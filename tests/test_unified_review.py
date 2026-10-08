import json
from pathlib import Path
import re
import shutil
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import library_review
from app.routes import pages, system
from tests.test_home_review import library  # noqa: F401 -- fixture


def test_review_has_batch_decisions_shared_files_and_one_check(library):
    app = FastAPI()
    app.include_router(pages.router)
    with TestClient(app) as client:
        response = client.get("/review")
        assert response.status_code == 200
        assert "data-library-check" in response.text
        assert "reviewKeepSelected" in response.text
        assert "checkHardlinks" in response.text
        assert "Open in Triage" not in response.text
        assert "Reviewed already" not in response.text
        reviewed = client.get("/review?show_resolved=1")
        assert "Reviewed already" in reviewed.text
        filtered = client.get("/review?classification=REJECT&reason_code=MISMATCH")
        match = re.search(r'<script id="reviewData" type="application/json">(.*?)</script>', filtered.text, re.S)
        items = json.loads(match[1])["items"]
        assert len(items) == 3
        assert all(item["classification"] == "REJECT" for item in items)


def test_unassigned_and_duplicates_share_the_workspace(library, monkeypatch):
    monkeypatch.setattr(pages, "stored_checks", lambda: [])
    monkeypatch.setattr(pages, "find_duplicates", lambda: [])
    app = FastAPI()
    app.include_router(pages.router)
    with TestClient(app) as client:
        for view in ("unmatched", "duplicates"):
            response = client.get("/review?view=" + view)
            assert response.status_code == 200
            assert "data-library-check" in response.text
            assert 'aria-label="Review queues"' in response.text
        for url in ("/review/unmatched", "/review/duplicates", "/review/table"):
            assert client.get(url).status_code == 200


def test_check_endpoint_requires_confirmation_and_reports_overlap(monkeypatch):
    starts = []
    monkeypatch.setattr(system, "start_library_check", lambda: starts.append(True) or "check-1")
    app = FastAPI()
    app.include_router(system.router)
    with TestClient(app) as client:
        assert client.post("/api/library-check", json={"confirm": "SCAN"}).status_code == 400
        assert starts == []
        response = client.post("/api/library-check", json={"confirm": "CHECK_LIBRARY"})
        assert response.json() == {"check_id": "check-1"}
        monkeypatch.setattr(system, "start_library_check", lambda: None)
        assert client.post("/api/library-check", json={"confirm": "CHECK_LIBRARY"}).status_code == 409


def test_mixed_audio_suggests_recording_review_instead_of_replacement():
    item = library_review.review_item({"id": 1, "reason_code": "MIXED_AUDIO_CONTENT"}, {"verdict": "WRONG_CONTENT"})
    assert item["mixedAudio"]
    assert "recordings" in item["suggestion"]
    assert "Replace" not in item["suggestion"]


def check_message(state):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = Path("static/library-check.js").read_text()
    match = re.search(r"  function checkMessage\(state\) \{.*?\n  \}", source, re.S)
    assert match
    script = match[0] + "\nprocess.stdout.write(JSON.stringify(checkMessage(" + json.dumps(state) + ")));"
    return json.loads(subprocess.run([node, "-e", script], check=True, capture_output=True, text=True).stdout)


def test_check_summary_excludes_postponements_and_keeps_original_summary():
    message = check_message({"status": "complete", "verification": {"processed": 100},
                             "verification_result": {"processed": 3, "postponed": 1, "cacheHits": 2, "postponedReason": "Catalogue unavailable"},
                             "unmatched_result": {"checked": 4}})
    assert "2 results verified" in message
    assert "2 saved proofs reused" in message
    assert "1 postponed: Catalogue unavailable" in message
    assert "4 unassigned files" in message


def test_running_job_is_visible_after_a_previous_check_failure():
    assert "Verifying" in check_message({"status": "failed", "busy": True, "verification": {"status": "running", "processed": 1, "total": 3}})
