from __future__ import annotations

from app.config import settings
from app.db import (
    add_result,
    create_ebook_admission,
    create_scan,
    finish_scan,
    init_local_db,
    local_conn,
    result_by_id,
    update_ebook_admission,
)
from app.observe import run_observe_cycle
from app.verifier import init_verification_db


def _seed_admission(tmp_path, *, status: str) -> str:
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_verification_db()

    create_scan("registration-conflict-plan", 1)
    add_result(
        "registration-conflict-plan",
        {
            "file_id": 501,
            "book_id": 701,
            "author": "Expected Author",
            "title": "Expected Book",
            "format": "ebook",
            "stored_path": "/data/media/books/Expected Book/Expected Book.epub",
            "local_path": "/books/Expected Book/Expected Book.epub",
            "classification": "PASS",
            "risk_score": 0,
            "reason_code": "OK",
            "reasons": [],
            "metadata": {},
        },
    )
    finish_scan("registration-conflict-plan")

    with local_conn() as conn:
        result_id = int(
            conn.execute(
                "SELECT id FROM scan_results WHERE scan_id='registration-conflict-plan'"
            ).fetchone()["id"]
        )
    result = result_by_id(result_id)
    assert result is not None
    admission_id = create_ebook_admission(
        result,
        "Expected Book/Expected Book.epub",
    )
    update_ebook_admission(
        admission_id,
        status,
        staged_sha256="a" * 64,
        publication_method="renameat2",
        error="exact path belongs to the wrong Bindery book",
    )
    return original


def test_registration_conflict_earns_nonexecuting_e3_plan(monkeypatch, tmp_path):
    original = _seed_admission(tmp_path, status="registration_conflict")
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        observed = run_observe_cycle()
    finally:
        settings.config_dir = original

    assert observed["decisionCount"] == 1
    record = observed["records"][0]
    assert record["subjectKind"] == "admission"
    assert record["decision"] == "would_correct_registration_conflict"
    assert record["reasonCode"] == "REGISTRATION_CONFLICT"

    assert observed["planCount"] == 1
    plan = observed["plans"][0]
    assert plan["subjectKind"] == "admission"
    assert plan["planKind"] == "CORRECT_REGISTRATION_CONFLICT"
    assert plan["reasonCode"] == "REGISTRATION_CONFLICT"
    assert plan["executionAllowed"] is False
    assert [step["code"] for step in plan["steps"]] == [
        "revalidate_registration_conflict",
        "correct_exact_registration_owner",
        "verify_registration_correction",
    ]
    assert plan["steps"][1]["externalMutation"] is True


def test_interrupted_registration_correction_remains_attention(monkeypatch, tmp_path):
    original = _seed_admission(tmp_path, status="registration_correcting")
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        observed = run_observe_cycle()
    finally:
        settings.config_dir = original

    assert observed["decisionCount"] == 1
    record = observed["records"][0]
    assert record["decision"] == "attention"
    assert record["reasonCode"] == "REGISTRATION_CORRECTION_INTERRUPTED"
    assert observed["planCount"] == 0
