from __future__ import annotations

import pytest

import app.registration_correction as correction


def _plan() -> dict:
    return {
        "id": 31,
        "signature": "plan-signature",
        "evidenceRevision": "evidence-revision",
        "planKind": "CORRECT_REGISTRATION_CONFLICT",
        "reasonCode": "REGISTRATION_CONFLICT",
        "subjectKind": "admission",
        "subjectId": "7",
        "resultId": 9,
        "bookId": 11,
        "path": "/data/media/books/Expected/Expected.epub",
    }


def _conflict_preview() -> dict:
    return {
        "safe": True,
        "adoptionSafe": False,
        "checks": {
            "workflowStateConflict": True,
            "publishedBytesCurrent": True,
            "stagedBytesCurrent": True,
            "exactForeignOwner": True,
            "queueIdentityCurrent": True,
            "reassignmentPreviewExactNoMove": True,
        },
        "admissionId": 7,
        "resultId": 9,
        "bookId": 11,
        "storedPath": "/data/media/books/Expected/Expected.epub",
        "stagedSha256": "a" * 64,
        "queueId": 77,
        "registrationState": "conflict",
    }


def test_revalidate_requires_current_e3_conflict_plan(monkeypatch):
    plan = _plan()
    executor = correction.RegistrationConflictExecutor()
    monkeypatch.setattr(correction, "ebook_admission_by_id", lambda admission_id: {"id": admission_id})
    monkeypatch.setattr(executor, "_current_plan", lambda admission_id: dict(plan))
    monkeypatch.setattr(
        correction,
        "registration_conflict_preview",
        lambda admission_id, client=None: _conflict_preview(),
    )

    boundary = executor.revalidate(plan, {"code": "correct_exact_registration_owner"})

    assert boundary["ok"] is True
    assert boundary["registrationState"] == "conflict"
    assert boundary["queueId"] == 77
    assert all(item["ok"] for item in boundary["checks"])


def test_interrupted_correction_adopts_only_proven_target_owner(monkeypatch):
    plan = _plan()
    executor = correction.RegistrationConflictExecutor()
    updates: list[tuple[int, str]] = []

    monkeypatch.setattr(
        correction,
        "registration_conflict_preview",
        lambda admission_id, client=None: {
            "adoptionSafe": True,
            "adoptionBlockers": [],
            "admissionId": 7,
            "bookId": 11,
            "registrationState": "corrected",
        },
    )
    monkeypatch.setattr(
        correction,
        "update_ebook_admission",
        lambda admission_id, status: updates.append((admission_id, status)),
    )
    monkeypatch.setattr(
        correction,
        "correct_registration_conflict",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("uncertain recovery must never replay correction")
        ),
    )

    result = executor.reconcile_uncertain(plan, {}, {"state": "running"})

    assert updates == [(7, "registered")]
    assert result["status"] == "registered"
    assert result["externalMutationPerformed"] is False
    assert result["reconciledAfterRestart"] is True


def test_interrupted_correction_blocks_when_target_owner_is_not_proven(monkeypatch):
    plan = _plan()
    executor = correction.RegistrationConflictExecutor()
    monkeypatch.setattr(
        correction,
        "registration_conflict_preview",
        lambda admission_id, client=None: {
            "adoptionSafe": False,
            "adoptionBlockers": ["exactTargetOwner"],
            "registrationState": "conflict",
        },
    )
    monkeypatch.setattr(
        correction,
        "correct_registration_conflict",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("uncertain recovery must never replay correction")
        ),
    )

    with pytest.raises(correction.AutomaticExecutionBlocked) as exc:
        executor.reconcile_uncertain(plan, {}, {"state": "running"})

    assert exc.value.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME"
    assert "will not be replayed" in str(exc.value)


def test_execute_preserves_no_delete_contract(monkeypatch):
    plan = _plan()
    executor = correction.RegistrationConflictExecutor()
    monkeypatch.setattr(
        correction,
        "correct_registration_conflict",
        lambda admission_id, client=None: {
            "admissionId": admission_id,
            "bookId": 11,
            "status": "registered",
            "alreadyCorrected": False,
            "queueRecordRemoved": True,
            "removedFromDownloadClient": False,
            "downloadedDataDeleted": False,
            "libraryBytesChanged": False,
            "stagedFileRetained": True,
        },
    )

    result = executor.execute(plan, {}, {})

    assert result["status"] == "registered"
    assert result["externalMutationPerformed"] is True
    assert result["removedFromDownloadClient"] is False
    assert result["downloadedDataDeleted"] is False
    assert result["libraryBytesChanged"] is False
    assert result["stagingRetained"] is True
