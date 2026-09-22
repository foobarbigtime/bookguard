from __future__ import annotations

from app.config import settings
from app.db import (
    add_result,
    create_ebook_acquisition,
    create_ebook_admission,
    create_scan,
    finish_scan,
    init_local_db,
    update_ebook_acquisition,
    update_ebook_admission,
)
from app.observe import run_observe_cycle
from app.verifier import init_verification_db


def test_observe_plans_real_failure_shapes_without_executing(monkeypatch, tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_verification_db()

    try:
        create_scan("workflow-recovery-scan", 1)
        add_result(
            "workflow-recovery-scan",
            {
                "file_id": 501,
                "book_id": 1169,
                "author": "Stephen King",
                "title": "Sometimes They Come Back",
                "format": "ebook",
                "stored_path": "/data/media/books/Stephen King/Sometimes They Come Back (1974)/Sometimes They Come Back - Stephen King.epub",
                "local_path": "/books/Stephen King/Sometimes They Come Back (1974)/Sometimes They Come Back - Stephen King.epub",
                "classification": "PASS",
                "risk_score": 0,
                "reason_code": "OK",
                "reasons": [],
                "metadata": {},
            },
        )
        finish_scan("workflow-recovery-scan")

        from app.db import latest_results
        result = latest_results(limit=1)[0]

        acquisition_id = create_ebook_acquisition(
            result,
            {
                "guid": "candidate-guid",
                "title": "Sometimes They Come Back - Stephen King",
                "indexerName": "Example",
                "protocol": "torrent",
            },
        )
        update_ebook_acquisition(
            acquisition_id,
            "failed",
            error=(
                'Bindery grab failed: Bindery POST /queue/grab returned HTTP 409: '
                '{"error":"already grabbed: this release has already been imported"}'
            ),
        )

        admission_id = create_ebook_admission(
            result,
            "Sometimes They Come Back - Stephen King.live-test.epub",
        )
        update_ebook_admission(
            admission_id,
            "failed",
            error=(
                "[Errno 22] Invalid argument: "
                "PosixPath('/admission-books/Stephen King/Sometimes They Come Back (1974)/"
                "Sometimes They Come Back - Stephen King.epub')"
            ),
        )

        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        observed = run_observe_cycle()

    finally:
        settings.config_dir = original

    workflow_records = [
        record
        for record in observed["records"]
        if record["subjectKind"] in {"acquisition", "admission"}
    ]
    assert len(workflow_records) == 2
    assert {r["decision"] for r in workflow_records} == {
        "would_recover_acquisition_failure",
        "would_recover_admission_failure",
    }

    assert observed["planCount"] == 2
    plans = {plan["planKind"]: plan for plan in observed["plans"]}

    acquisition = plans["SELECT_ALTERNATE_REPLACEMENT"]
    assert acquisition["executionAllowed"] is False
    assert acquisition["reasonCode"] == "ACQUISITION_RELEASE_ALREADY_IMPORTED"
    assert acquisition["preconditions"]["retryPolicy"] == {
        "retrySameOperation": False,
        "maxRetries": 0,
        "backoffSeconds": [],
    }
    assert "WORKFLOW_STATE_UNCHANGED" in acquisition["preconditions"]["requiredChecks"]

    admission = plans["RECOVER_ADMISSION_PUBLICATION"]
    assert admission["executionAllowed"] is False
    assert admission["reasonCode"] == "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED"
    assert admission["preconditions"]["retryPolicy"]["retrySameOperation"] is False

    admission_steps = {step["code"]: step for step in admission["steps"]}
    assert admission_steps["retry_guarded_publication"]["externalMutation"] is True
    assert admission_steps["retry_guarded_publication"]["stopIfUnproven"] is True


def test_unknown_workflow_failure_stays_attention(monkeypatch, tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_verification_db()

    try:
        create_scan("unknown-failure-scan", 1)
        add_result(
            "unknown-failure-scan",
            {
                "file_id": 601,
                "book_id": 2001,
                "author": "Example Author",
                "title": "Example Book",
                "format": "ebook",
                "stored_path": "/data/media/books/Example Book.epub",
                "local_path": "/books/Example Book.epub",
                "classification": "PASS",
                "risk_score": 0,
                "reason_code": "OK",
                "reasons": [],
                "metadata": {},
            },
        )
        finish_scan("unknown-failure-scan")

        from app.db import latest_results
        result = latest_results(limit=1)[0]

        acquisition_id = create_ebook_acquisition(
            result,
            {
                "guid": "candidate-guid",
                "title": "Example Book",
                "indexerName": "Example",
                "protocol": "torrent",
            },
        )
        update_ebook_acquisition(
            acquisition_id,
            "failed",
            error="Unknown failure shape that BookGuard cannot safely classify.",
        )

        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        observed = run_observe_cycle()

    finally:
        settings.config_dir = original

    workflow = [
        record for record in observed["records"]
        if record["subjectKind"] == "acquisition"
    ]
    assert len(workflow) == 1
    assert workflow[0]["decision"] == "attention"
    assert workflow[0]["reasonCode"] == "ACQUISITION_REVIEW_REQUIRED"
    assert observed["planCount"] == 0
