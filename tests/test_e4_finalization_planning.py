from __future__ import annotations

from app.config import settings
from app.db import (
    add_result,
    create_ebook_acquisition,
    create_ebook_admission,
    create_scan,
    finish_scan,
    init_local_db,
    local_conn,
    update_ebook_acquisition,
    update_ebook_admission,
)
from app.observe import _acquisition_decisions
from app.recovery_planner import _build_plan


STAGED_PATH = "BookGuard Test/Finalization Fixture.epub"
STAGED_SHA = "a" * 64


def _seed_admitted_acquisition(tmp_path, *, admission_status: str, admission_sha: str = STAGED_SHA):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()

    create_scan("finalization-planning-scan", 1)
    add_result(
        "finalization-planning-scan",
        {
            "file_id": 4101,
            "book_id": 4201,
            "author": "Finalization Author",
            "title": "Finalization Fixture",
            "format": "ebook",
            "stored_path": "/data/media/books/BookGuard Test/Finalization Fixture.epub",
            "local_path": "/books/BookGuard Test/Finalization Fixture.epub",
            "classification": "REVIEW",
            "risk_score": 50,
            "reason_code": "FINALIZATION_FIXTURE",
            "reasons": ["fixture"],
            "metadata": {},
        },
    )
    finish_scan("finalization-planning-scan")

    with local_conn() as conn:
        result = dict(
            conn.execute(
                "SELECT * FROM scan_results WHERE scan_id='finalization-planning-scan'"
            ).fetchone()
        )

    acquisition_id = create_ebook_acquisition(
        result,
        {
            "guid": "finalization-guid",
            "title": "Finalization Fixture release",
            "indexerName": "Fixture",
            "protocol": "usenet",
        },
    )
    admission_id = create_ebook_admission(result, STAGED_PATH)
    update_ebook_admission(
        admission_id,
        admission_status,
        staged_sha256=admission_sha,
    )
    update_ebook_acquisition(
        acquisition_id,
        "admitted",
        staged_relative_path=STAGED_PATH,
        staged_sha256=STAGED_SHA,
        admission_id=admission_id,
    )
    return original, acquisition_id, admission_id


def test_registered_linked_admission_creates_finalization_plan(tmp_path):
    original, acquisition_id, admission_id = _seed_admitted_acquisition(
        tmp_path,
        admission_status="registered",
    )
    try:
        with local_conn() as conn:
            decisions = _acquisition_decisions(conn, 100)
            decision = next(
                item
                for item in decisions
                if item["subjectKind"] == "acquisition"
                and int(item["subjectId"]) == acquisition_id
            )
            plan = _build_plan(conn, decision)
    finally:
        settings.config_dir = original

    assert decision["decision"] == "would_finalize_acquisition"
    assert decision["reasonCode"] == "FINALIZATION_RECOVERY_AVAILABLE"
    assert decision["evidence"]["admissionId"] == admission_id
    assert decision["evidence"]["admissionStatus"] == "registered"
    assert decision["evidence"]["linkedAdmissionIdentityMatches"] is True
    assert plan is not None
    assert plan["planKind"] == "FINALIZE_ACQUISITION"
    assert [step["code"] for step in plan["steps"]] == [
        "revalidate_finalization_state",
        "finish_guarded_cleanup",
    ]
    assert plan["steps"][0]["externalMutation"] is False
    assert plan["steps"][1]["externalMutation"] is True


def test_pending_linked_admission_still_plans_registration_reconciliation(tmp_path):
    original, acquisition_id, admission_id = _seed_admitted_acquisition(
        tmp_path,
        admission_status="scan_requested",
    )
    try:
        with local_conn() as conn:
            decision = next(
                item
                for item in _acquisition_decisions(conn, 100)
                if item["subjectKind"] == "acquisition"
                and int(item["subjectId"]) == acquisition_id
            )
    finally:
        settings.config_dir = original

    assert decision["decision"] == "would_reconcile_admission"
    assert decision["reasonCode"] == "ADMISSION_REGISTRATION_PENDING"
    assert decision["evidence"]["admissionId"] == admission_id
    assert decision["evidence"]["admissionStatus"] == "scan_requested"


def test_registered_link_with_mismatched_staged_identity_requires_attention(tmp_path):
    original, acquisition_id, _ = _seed_admitted_acquisition(
        tmp_path,
        admission_status="registered",
        admission_sha="b" * 64,
    )
    try:
        with local_conn() as conn:
            decision = next(
                item
                for item in _acquisition_decisions(conn, 100)
                if item["subjectKind"] == "acquisition"
                and int(item["subjectId"]) == acquisition_id
            )
    finally:
        settings.config_dir = original

    assert decision["decision"] == "attention"
    assert decision["reasonCode"] == "ACQUISITION_ADMISSION_IDENTITY_MISMATCH"
    assert decision["evidence"]["admissionStatus"] == "registered"
    assert decision["evidence"]["linkedAdmissionIdentityMatches"] is False
