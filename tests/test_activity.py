"""The Activity timeline: plain sentences, who and result, folding, filters."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import change_log
from app.activity import activity_events, activity_totals, filter_events
from app.catalogue_move import init_moves_db
from app.config import settings
from app.db import (
    add_result,
    create_cleanup_action,
    create_metadata_repair,
    create_scan,
    finish_cleanup_action,
    finish_metadata_repair,
    finish_scan,
    init_local_db,
    local_conn,
    result_by_id,
)
from app.routes.pages import router as pages_router
from app.routes.system import router as system_router
from app.verifier import init_verification_db


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    init_local_db()
    init_verification_db()
    create_scan("scan-1", 2)
    for index, title in enumerate(["Mary, Mary", "Sooley"], start=1):
        add_result("scan-1", {
            "file_id": index, "book_id": index, "author": "James Patterson", "title": title, "format": "ebook",
            "stored_path": f"/data/media/books/{title}.epub", "local_path": f"/books/{title}.epub",
            "classification": "REJECT", "risk_score": 90, "reason_code": "MISMATCH", "reasons": [], "metadata": {},
        })
    finish_scan("scan-1")
    with local_conn() as conn:
        ids = {row["title"]: row["id"] for row in conn.execute("SELECT id, title FROM scan_results")}
        for title, verdict in [("Mary, Mary", "WRONG_CONTENT"), ("Sooley", "METADATA_ERROR")]:
            conn.execute(
                """INSERT INTO content_verifications(signature, result_id, scan_id, file_id, book_id, format,
                       author, title, target_path, file_fingerprint, verdict, confidence, source, evidence_json,
                       created_at, updated_at)
                   VALUES (?, ?, 'scan-1', 1, 1, 'ebook', 'James Patterson', ?, '/x', 'f', ?, 90, 'native', '{}',
                       '2026-10-02T10:00:00+00:00', '2026-10-02T10:00:00+00:00')""",
                (f"sig-{title}", ids[title], title, verdict),
            )
        conn.commit()
    quarantine = create_cleanup_action(result_by_id(ids["Mary, Mary"]), "TRIAGE_QUARANTINE")
    finish_cleanup_action(quarantine, "applied")
    repair = create_metadata_repair(result_by_id(ids["Sooley"]), "ebook_metadata", {"title": "x"}, {"title": "Sooley"})
    finish_metadata_repair(repair, "applied")
    init_moves_db()
    with local_conn() as conn:
        conn.execute(
            """INSERT INTO catalogue_moves(result_id, source_book_id, target_book_id, target_title, source_path,
                   destination, sha256, status, created_at, updated_at)
               VALUES (?, 1, 2, 'Agenten', '/data/media/books/Broker ()/Broker.epub', '/data/media/books/Agenten ()/Agenten.epub',
                   'sha256:a', 'pending', '2026-10-02T11:00:00+00:00', '2026-10-02T11:00:00+00:00')""",
            (ids["Mary, Mary"],),
        )
        conn.commit()
    return {"ids": ids, "repair": repair}


def by_kind(events, kind):
    return [event for event in events if event["kind"] == kind]


def test_records_become_plain_sentences_with_who_and_result(journal):
    events = activity_events()
    quarantine = by_kind(events, "cleanup")[0]
    assert quarantine["sentence"] == "You quarantined “Mary, Mary” (nothing was deleted)"
    assert (quarantine["who"], quarantine["result"]) == ("you", "done")

    repair = by_kind(events, "repair")[0]
    assert repair["sentence"] == "You fixed the details of “Sooley”"
    assert repair["actions"] == [{"kind": "undo-repair", "id": journal["repair"], "label": "Undo"}]

    move = by_kind(events, "catalogue_move")[0]
    assert move["sentence"] == "Moving a misfiled ebook to “Agenten” is waiting for Bindery"
    assert move["result"] == "waiting"

    scan = by_kind(events, "scan")[0]
    assert scan["sentence"] == "Library scan finished: 2 books checked"


def test_per_file_verifications_fold_into_one_event_per_day(journal):
    checks = by_kind(activity_events(), "verification")
    assert len(checks) == 1
    assert checks[0]["sentence"] == "Checked 2 library files: 1 wrong file, 1 wrong details"
    assert len(checks[0]["items"]) == 2


def test_filters_by_who_result_text_and_period(journal):
    events = activity_events()
    assert {e["who"] for e in filter_events(events, who="you")} == {"you"}
    assert [e["kind"] for e in filter_events(events, result="problems")] == ["catalogue_move"]
    assert [e["kind"] for e in filter_events(events, query="agenten")] == ["catalogue_move"]
    later = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    recent = filter_events(events, period="1", now=later)
    assert recent and all(e["timestamp"] >= "2026-10-01T12:00" for e in recent)
    assert filter_events(events, period="1", now=datetime(2027, 1, 1, tzinfo=timezone.utc)) == []
    assert activity_totals(events)["waiting"] == 1


def test_settings_changes_are_recorded_with_masked_secrets(journal):
    before = {"allow_actions": False, "bindery_api_key_set": False, "author_aliases": ["A = B"], "sample_files": 3}
    after = {"allow_actions": True, "bindery_api_key_set": True, "author_aliases": ["A = B", "C = D"], "sample_files": 3}
    assert change_log.record_settings_change(before, before) is None
    change_log.record_settings_change(before, after)
    event = by_kind(activity_events(), "settings")[0]
    assert event["sentence"] == "You changed 3 settings"
    assert [item["text"] for item in event["items"]] == [
        "Bindery actions: off → on",
        "Pen names: added C = D",
        "Bindery API key changed",
    ]


def test_a_start_on_a_new_revision_reads_as_an_update(journal, monkeypatch):
    monkeypatch.setenv("BOOKGUARD_BUILD_REVISION", "eb21609c597c")
    change_log.record_app_start()
    change_log.record_app_start()
    monkeypatch.setenv("BOOKGUARD_BUILD_REVISION", "6a7fb49b9079")
    change_log.record_app_start()
    sentences = [e["sentence"] for e in by_kind(activity_events(), "app_start")]
    version = f"v{change_log.__version__}"
    assert sentences == [
        f"BookGuard was updated to {version} (6a7fb49)",
        f"BookGuard {version} (eb21609) restarted",
        f"BookGuard {version} (eb21609) started for the first time",
    ]


def test_saving_settings_records_what_changed(journal, monkeypatch):
    monkeypatch.setattr(settings, "allow_actions", False)
    app = FastAPI()
    app.include_router(system_router)
    with TestClient(app) as client:
        assert client.post("/api/settings", json={"allow_actions": True}).status_code == 200
    event = by_kind(activity_events(), "settings")[0]
    assert event["sentence"] == "You changed a setting: Bindery actions: off → on"


def test_activity_page_and_api_render_the_timeline(journal):
    app = FastAPI()
    app.include_router(pages_router)
    app.include_router(system_router)
    with TestClient(app) as client:
        page = client.get("/activity").text
        assert "You quarantined “Mary, Mary” (nothing was deleted)" in page
        assert f'data-undo-repair="{journal["repair"]}"' in page
        assert "Waiting for you" in page
        filtered = client.get("/activity?result=problems").text
        assert "is waiting for Bindery" in filtered and "You quarantined" not in filtered
        data = client.get("/api/activity?who=you").json()
        assert data["totals"]["events"] == len(data["events"]) > 0
