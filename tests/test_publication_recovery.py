from __future__ import annotations

from contextlib import contextmanager
import sqlite3

import pytest

from app import publication_recovery as recovery


def _plan() -> dict:
    codes = (
        "revalidate_failed_admission", "revalidate_staged_bytes",
        "revalidate_admission_topology", "prove_supported_no_replace_method",
        "retry_guarded_publication", "scan_and_reconcile_registration",
    )
    return {
        "id": 12, "subjectId": 9, "subjectKind": "admission",
        "resultId": 4, "bookId": 101, "path": "/data/media/books/Fixture.epub",
        "signature": "exact-plan", "evidenceRevision": "revision-1",
        "planKind": "RECOVER_ADMISSION_PUBLICATION",
        "reasonCode": "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED",
        "currentStep": 4,
        "steps": [{"code": code, "externalMutation": index >= 3}
                  for index, code in enumerate(codes)],
    }


def _boundary() -> dict:
    return {
        "ok": True, "planSignature": "exact-plan", "evidenceRevision": "revision-1",
        "checks": [{"code": "CURRENT", "ok": True}],
        "destinationParent": "/admission-books", "parentDevice": 7,
        "parentInode": 8, "stagedSha256": "a" * 64,
    }


def _receipt() -> dict:
    return {
        "state": "succeeded", "boundary": _boundary(),
        "externalResult": {"admissionId": 9, "publicationMethod": "renameat2",
                           "admissionPublished": False, "binderyScanRequested": False},
    }


def test_publication_requires_exact_completed_proof(monkeypatch):
    monkeypatch.setattr(recovery.core, "_existing", lambda *args: None)
    with pytest.raises(recovery.core.AutomaticExecutionBlocked, match="no complete"):
        recovery._EXECUTOR.revalidate(_plan(), _plan()["steps"][4])


def test_publication_refuses_changed_filesystem_proof(monkeypatch):
    monkeypatch.setattr(recovery.core, "_existing", lambda *args: _receipt())
    monkeypatch.setattr(recovery.publication_proof._EXECUTOR, "revalidate",
                        lambda plan, step: {**_boundary(), "parentInode": 99})
    boundary = recovery._EXECUTOR.revalidate(_plan(), _plan()["steps"][4])
    assert boundary["ok"] is False
    assert boundary["checks"][-1] == {
        "code": "CURRENT_FILESYSTEM_PROOF", "ok": False
    }


def test_interrupted_publication_refuses_different_running_boundary(monkeypatch):
    monkeypatch.setattr(recovery, "_proven_receipt", lambda plan: _boundary())
    monkeypatch.setattr(recovery, "ebook_admission_by_id", lambda admission_id:
                        pytest.fail("changed boundary must stop before admission reads"))
    running = {"boundary": {**_boundary(), "parentInode": 100}}
    with pytest.raises(recovery.core.AutomaticExecutionBlocked,
                       match="boundary differs"):
        recovery._EXECUTOR.reconcile_uncertain(
            _plan(), _plan()["steps"][4], running
        )


def test_publication_state_transition_is_exact_and_clears_old_failure(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.execute("""
        CREATE TABLE ebook_admissions (
            id INTEGER, status TEXT, publication_method TEXT, failure_stage TEXT,
            error TEXT, updated_at TEXT, staged_sha256 TEXT, result_id INTEGER,
            book_id INTEGER, stored_path TEXT
        )
    """)
    connection.execute(
        "INSERT INTO ebook_admissions VALUES (9, 'failed', NULL, "
        "'no_replace_unsupported', 'unsupported', '', ?, 4, 101, ?)",
        ("a" * 64, _plan()["path"]),
    )
    connection.commit()

    @contextmanager
    def local_conn():
        yield connection

    monkeypatch.setattr(recovery, "local_conn", local_conn)
    monkeypatch.setattr(recovery, "utc_now", lambda: "now")
    with pytest.raises(recovery.AdmissionSafetyError, match="state changed"):
        recovery._record_publication({**_plan(), "stagedSha256": "b" * 64}, "renameat2")
    recovery._record_publication({**_plan(), "stagedSha256": "a" * 64}, "renameat2")
    row = connection.execute(
        "SELECT status, publication_method, failure_stage, error FROM ebook_admissions"
    ).fetchone()
    assert row == ("published", "renameat2", None, None)
    with pytest.raises(recovery.AdmissionSafetyError, match="state changed"):
        recovery._record_publication({**_plan(), "stagedSha256": "a" * 64}, "renameat2")
    connection.close()


def test_scan_step_stays_inert(monkeypatch):
    plan = {**_plan(), "currentStep": 5}
    monkeypatch.setattr(recovery.core, "attempt_automatic_step", lambda plan_id:
                        pytest.fail("scan must remain disabled"))
    result = recovery.run_publication_recovery_cycle(plan)
    assert result["state"] == "paused"
    assert result["externalMutationAttempted"] is False
