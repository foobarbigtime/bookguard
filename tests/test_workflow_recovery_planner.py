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
from app.recovery_planner import (
    promote_due_recovery_retries,
    recovery_plan_by_id,
    recovery_plan_transition_snapshot,
    schedule_recovery_retry,
)
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
            staged_sha256="a" * 64,
            verification={"safeToAdmit": True, "sha256": "a" * 64},
            failure_stage="no_replace_unsupported",
            error=(
                "[Errno 22] Invalid argument: "
                "PosixPath('/admission-books/Stephen King/Sometimes They Come Back (1974)/"
                "Sometimes They Come Back - Stephen King.epub')"
            ),
        )

        ambiguous_id = create_ebook_admission(result, "unknown-scan-outcome.epub")
        update_ebook_admission(
            ambiguous_id,
            "failed",
            staged_sha256="b" * 64,
            verification={"safeToAdmit": True, "sha256": "b" * 64},
            error="Bindery scan timed out after request was sent",
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
    assert len(workflow_records) == 3
    assert {r["decision"] for r in workflow_records} == {
        "would_recover_acquisition_failure",
        "would_recover_admission_failure",
        "attention",
    }
    ambiguous = next(r for r in workflow_records if r["subjectId"] == ambiguous_id)
    assert ambiguous["decision"] == "attention"
    assert ambiguous["reasonCode"] == "ADMISSION_FAILED"

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


def test_transient_retry_schedule_is_bounded_and_durable(monkeypatch, tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_verification_db()

    try:
        create_scan("transient-retry-scan", 1)
        add_result(
            "transient-retry-scan",
            {
                "file_id": 701,
                "book_id": 3001,
                "author": "Retry Author",
                "title": "Retry Book",
                "format": "ebook",
                "stored_path": "/data/media/books/Retry Book.epub",
                "local_path": "/books/Retry Book.epub",
                "classification": "PASS",
                "risk_score": 0,
                "reason_code": "OK",
                "reasons": [],
                "metadata": {},
            },
        )
        finish_scan("transient-retry-scan")

        from app.db import latest_results
        result = latest_results(limit=1)[0]

        acquisition_id = create_ebook_acquisition(
            result,
            {
                "guid": "retry-guid",
                "title": "Retry Book",
                "indexerName": "Example",
                "protocol": "torrent",
            },
        )
        update_ebook_acquisition(
            acquisition_id,
            "failed",
            error="Bindery POST /queue/grab returned HTTP 503: temporarily unavailable",
        )

        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        observed = run_observe_cycle()
        plan = observed["plans"][0]

        first = schedule_recovery_retry(
            plan["id"],
            "first retry scheduled",
            now="2026-09-22T01:00:00+00:00",
        )
        assert first["state"] == "retry_wait"
        assert first["retryCount"] == 1
        assert first["nextRetryAt"] == "2026-09-22T01:00:30+00:00"

        # A fresh read proves retry state is durable, not in-memory.
        reread = recovery_plan_by_id(plan["id"])
        assert reread is not None
        assert reread["state"] == "retry_wait"
        assert reread["retryCount"] == 1
        assert reread["nextRetryAt"] == "2026-09-22T01:00:30+00:00"

        assert promote_due_recovery_retries(
            now="2026-09-22T01:00:29+00:00"
        ) == []

        promoted = promote_due_recovery_retries(
            now="2026-09-22T01:00:30+00:00"
        )
        assert [item["id"] for item in promoted] == [plan["id"]]
        assert promoted[0]["state"] == "ready"
        assert promoted[0]["nextRetryAt"] is None

        second = schedule_recovery_retry(
            plan["id"],
            "second retry scheduled",
            now="2026-09-22T01:01:00+00:00",
        )
        assert second["retryCount"] == 2
        assert second["nextRetryAt"] == "2026-09-22T01:03:00+00:00"

        promote_due_recovery_retries(now="2026-09-22T01:03:00+00:00")
        third = schedule_recovery_retry(
            plan["id"],
            "third retry scheduled",
            now="2026-09-22T01:04:00+00:00",
        )
        assert third["retryCount"] == 3
        assert third["nextRetryAt"] == "2026-09-22T01:09:00+00:00"

        promote_due_recovery_retries(now="2026-09-22T01:09:00+00:00")
        exhausted = schedule_recovery_retry(
            plan["id"],
            "fourth retry refused",
            now="2026-09-22T01:10:00+00:00",
        )
        assert exhausted["state"] == "blocked"
        assert exhausted["retryCount"] == 3
        assert exhausted["nextRetryAt"] is None
        assert "Retry budget exhausted" in exhausted["lastError"]

        transitions = recovery_plan_transition_snapshot(plan["id"])
        events = [item["event"] for item in transitions]
        assert events == [
            "created",
            "retry_scheduled",
            "retry_due",
            "retry_scheduled",
            "retry_due",
            "retry_scheduled",
            "retry_due",
            "retry_budget_exhausted",
        ]
        assert transitions[-1]["toState"] == "blocked"
        assert transitions[-1]["toStep"] == plan["currentStep"]

    finally:
        settings.config_dir = original


def test_nonretryable_plan_cannot_be_scheduled(monkeypatch, tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_verification_db()

    try:
        create_scan("nonretryable-scan", 1)
        add_result(
            "nonretryable-scan",
            {
                "file_id": 801,
                "book_id": 4001,
                "author": "Example Author",
                "title": "Already Imported",
                "format": "ebook",
                "stored_path": "/data/media/books/Already Imported.epub",
                "local_path": "/books/Already Imported.epub",
                "classification": "PASS",
                "risk_score": 0,
                "reason_code": "OK",
                "reasons": [],
                "metadata": {},
            },
        )
        finish_scan("nonretryable-scan")

        from app.db import latest_results
        result = latest_results(limit=1)[0]

        acquisition_id = create_ebook_acquisition(
            result,
            {
                "guid": "already-guid",
                "title": "Already Imported",
                "indexerName": "Example",
                "protocol": "torrent",
            },
        )
        update_ebook_acquisition(
            acquisition_id,
            "failed",
            error=(
                'Bindery POST /queue/grab returned HTTP 409: '
                '{"error":"already grabbed: this release has already been imported"}'
            ),
        )

        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        observed = run_observe_cycle()
        plan = observed["plans"][0]

        import pytest
        with pytest.raises(RuntimeError, match="does not authorize"):
            schedule_recovery_retry(
                plan["id"],
                "must not retry same candidate",
                now="2026-09-22T01:00:00+00:00",
            )
    finally:
        settings.config_dir = original
