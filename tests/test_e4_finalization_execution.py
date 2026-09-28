from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import automatic_execution as core
from app import automatic_runner as runner
from app import finalization


ALL_PREVIEW_CHECKS = {
    "actionsEnabled": True,
    "automaticReacquisitionEnabled": True,
    "workflowStateFinalizable": True,
    "linkedAdmissionRegistered": True,
    "acquisitionAdmissionIdentityConsistent": True,
    "resultIdentityConsistent": True,
    "libraryBytesCurrent": True,
    "binderyRegistrationCurrent": True,
    "queueResponseComplete": True,
    "queueIdPresent": True,
    "queueIdentityUnambiguous": True,
    "queueIdentityCurrent": True,
    "queueStatusFinalizable": True,
    "stagingStateSafe": True,
    "cleanupStateConsistent": True,
}


def _plan(*, plan_id=71, current_step=0, state="ready"):
    return {
        "id": plan_id,
        "signature": "finalize-signature",
        "evidenceRevision": "finalize-revision",
        "planKind": "FINALIZE_ACQUISITION",
        "reasonCode": "FINALIZATION_RECOVERY_AVAILABLE",
        "subjectKind": "acquisition",
        "subjectId": "81",
        "resultId": 91,
        "bookId": 101,
        "path": "BookGuard Test/Finalization Fixture.epub",
        "state": state,
        "currentStep": current_step,
        "steps": [
            {"code": "revalidate_finalization_state", "externalMutation": False},
            {"code": "finish_guarded_cleanup", "externalMutation": True},
        ],
    }


def _preview(*, cleanup_state="queue_and_staging_pending", queue=True, staged=True):
    return {
        "safe": True,
        "checks": dict(ALL_PREVIEW_CHECKS),
        "acquisitionId": 81,
        "admissionId": 82,
        "resultId": 91,
        "bookId": 101,
        "stagedRelativePath": "BookGuard Test/Finalization Fixture.epub",
        "stagedSha256": "a" * 64,
        "queueId": 77,
        "queuePresent": queue,
        "stagedPresent": staged,
        "cleanupState": cleanup_state,
    }


def test_finalization_executor_revalidates_exact_current_plan(monkeypatch):
    plan = _plan()
    executor = runner._FinalizationCleanupExecutor()

    monkeypatch.setattr(runner, "ebook_acquisition_by_id", lambda acquisition_id: {"id": 81})
    monkeypatch.setattr(executor, "_current_plan", lambda acquisition_id: dict(plan))
    monkeypatch.setattr(runner, "finalization_preview", lambda acquisition_id: _preview())

    boundary = executor.revalidate(plan, plan["steps"][0])

    assert boundary["ok"] is True
    assert boundary["cleanupState"] == "queue_and_staging_pending"
    assert boundary["queuePresent"] is True
    assert boundary["stagedPresent"] is True
    assert all(check["ok"] for check in boundary["checks"])


def test_finalization_executor_blocks_when_current_plan_authority_changed(monkeypatch):
    plan = _plan()
    changed = {**plan, "signature": "different"}
    executor = runner._FinalizationCleanupExecutor()

    monkeypatch.setattr(runner, "ebook_acquisition_by_id", lambda acquisition_id: {"id": 81})
    monkeypatch.setattr(executor, "_current_plan", lambda acquisition_id: changed)
    monkeypatch.setattr(runner, "finalization_preview", lambda acquisition_id: _preview())

    boundary = executor.revalidate(plan, plan["steps"][0])

    assert boundary["ok"] is False
    failed = {item["code"] for item in boundary["checks"] if not item["ok"]}
    assert "DECISION_STILL_AUTHORIZED" in failed


def test_finalization_execute_reports_no_mutation_when_cleanup_already_complete(monkeypatch):
    executor = runner._FinalizationCleanupExecutor()
    plan = _plan(current_step=1)
    boundary = {
        "cleanupState": "complete",
        "queuePresent": False,
        "stagedPresent": False,
    }
    monkeypatch.setattr(
        runner,
        "finalize_ebook_acquisition",
        lambda acquisition_id: {
            "acquisition": {"id": acquisition_id, "status": "finalized"},
            "cleanupAlreadyComplete": True,
            "removedFromDownloadClient": False,
            "downloadedDataDeleted": False,
        },
    )

    outcome = executor.execute(plan, plan["steps"][1], boundary)

    assert outcome["status"] == "finalized"
    assert outcome["externalMutationPerformed"] is False
    assert outcome["queueMutationPerformed"] is False
    assert outcome["stagedMutationPerformed"] is False
    assert outcome["libraryBytesChanged"] is False


def test_interrupted_finalization_resumes_only_monotonic_remaining_cleanup(monkeypatch):
    executor = runner._FinalizationCleanupExecutor()
    plan = _plan(current_step=1)
    existing = {
        "state": "running",
        "boundary": {
            "queuePresent": True,
            "stagedPresent": True,
            "cleanupState": "queue_and_staging_pending",
        },
    }
    monkeypatch.setattr(runner, "ebook_acquisition_by_id", lambda acquisition_id: {"id": 81})
    monkeypatch.setattr(
        runner,
        "finalization_preview",
        lambda acquisition_id: _preview(
            cleanup_state="staging_pending",
            queue=False,
            staged=True,
        ),
    )
    monkeypatch.setattr(
        runner,
        "finalize_ebook_acquisition",
        lambda acquisition_id: {
            "acquisition": {"id": acquisition_id, "status": "finalized"},
            "removedFromDownloadClient": False,
            "downloadedDataDeleted": False,
        },
    )

    outcome = executor.reconcile_uncertain(plan, plan["steps"][1], existing)

    assert outcome["reconciledAfterRestart"] is True
    assert outcome["queueMutationPerformed"] is False
    assert outcome["stagedMutationPerformed"] is True
    assert outcome["externalMutationPerformed"] is True


def test_interrupted_finalization_blocks_if_removed_queue_record_reappears(monkeypatch):
    executor = runner._FinalizationCleanupExecutor()
    plan = _plan(current_step=1)
    existing = {
        "state": "running",
        "boundary": {
            "queuePresent": False,
            "stagedPresent": True,
            "cleanupState": "staging_pending",
        },
    }
    monkeypatch.setattr(runner, "ebook_acquisition_by_id", lambda acquisition_id: {"id": 81})
    monkeypatch.setattr(runner, "finalization_preview", lambda acquisition_id: _preview())
    monkeypatch.setattr(
        runner,
        "finalize_ebook_acquisition",
        lambda acquisition_id: (_ for _ in ()).throw(
            AssertionError("regressed cleanup state must not execute")
        ),
    )

    with pytest.raises(core.AutomaticExecutionBlocked) as exc:
        executor.reconcile_uncertain(plan, plan["steps"][1], existing)

    assert exc.value.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME"
    assert "reappeared" in str(exc.value)


def test_runner_selects_oldest_finalization_plan_before_core(monkeypatch):
    finalize_plan = _plan(plan_id=5)
    retry_plan = {
        "id": 6,
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "subjectKind": "acquisition",
        "state": "ready",
    }
    monkeypatch.setattr(
        runner,
        "load_automation_settings",
        lambda: SimpleNamespace(automation_mode="automatic"),
    )
    monkeypatch.setattr(
        core,
        "recovery_plan_snapshot",
        lambda limit: {"items": [retry_plan, finalize_plan]},
    )
    monkeypatch.setattr(
        runner,
        "_run_finalization_cycle",
        lambda plan: {"state": "executed", "planId": plan["id"]},
    )
    monkeypatch.setattr(
        core,
        "run_automatic_cycle",
        lambda limit: (_ for _ in ()).throw(AssertionError("core runner should not win ordering")),
    )

    result = runner.run_automatic_cycle()

    assert result == {"state": "executed", "planId": 5}


def test_runner_delegates_when_older_supported_plan_is_not_finalization(monkeypatch):
    retry_plan = {
        "id": 4,
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "subjectKind": "acquisition",
        "state": "ready",
    }
    finalize_plan = _plan(plan_id=5)
    monkeypatch.setattr(
        runner,
        "load_automation_settings",
        lambda: SimpleNamespace(automation_mode="automatic"),
    )
    monkeypatch.setattr(
        core,
        "recovery_plan_snapshot",
        lambda limit: {"items": [finalize_plan, retry_plan]},
    )
    monkeypatch.setattr(
        core,
        "run_automatic_cycle",
        lambda limit, selected_plan=None: {"state": "core", "limit": limit,
            "selectedId": selected_plan["id"]},
    )

    result = runner.run_automatic_cycle(33)

    assert result == {"state": "core", "limit": 33, "selectedId": 4}


def test_finalization_preview_proves_exact_queue_and_bytes(monkeypatch, tmp_path):
    library = tmp_path / "library.epub"
    staged = tmp_path / "staged.epub"
    library.write_bytes(b"fixture")
    staged.write_bytes(b"fixture")
    expected = "a" * 64

    acquisition = {
        "id": 81,
        "result_id": 91,
        "book_id": 101,
        "status": "cleanup_required",
        "admission_id": 82,
        "staged_relative_path": "Fixture.epub",
        "staged_sha256": expected,
        "queue_id": 77,
        "candidate_title": "Expected Release",
        "candidate_protocol": "usenet",
    }
    admission = {
        "id": 82,
        "result_id": 91,
        "book_id": 101,
        "status": "registered",
        "staged_relative_path": "Fixture.epub",
        "staged_sha256": expected,
        "stored_path": "/data/media/books/Fixture.epub",
        "local_path": str(library),
    }
    result = {
        "id": 91,
        "stored_path": admission["stored_path"],
        "local_path": admission["local_path"],
    }

    class Client:
        def get_book(self, book_id):
            return {
                "id": book_id,
                "bookFiles": [
                    {"format": "ebook", "path": admission["stored_path"]}
                ],
            }

    monkeypatch.setattr(finalization, "ebook_acquisition_by_id", lambda acquisition_id: acquisition)
    monkeypatch.setattr(finalization, "ebook_admission_by_id", lambda admission_id: admission)
    monkeypatch.setattr(finalization, "result_by_id", lambda result_id: result)
    monkeypatch.setattr(
        finalization,
        "_automation_settings",
        lambda: SimpleNamespace(automatic_reacquisition=True),
    )
    monkeypatch.setattr(finalization.settings, "allow_actions", True)
    monkeypatch.setattr(finalization, "sha256_file", lambda path: expected)
    monkeypatch.setattr(finalization, "resolve_staged_file", lambda relative: (tmp_path, staged))
    monkeypatch.setattr(
        finalization,
        "_queue_payload",
        lambda client: ([{
            "id": 77,
            "bookId": 101,
            "title": "Expected Release",
            "protocol": "usenet",
            "status": "imported",
        }], False),
    )

    preview = finalization.finalization_preview(81, Client())

    assert preview["safe"] is True
    assert preview["cleanupState"] == "queue_and_staging_pending"
    assert preview["checks"]["queueIdentityCurrent"] is True


def test_finalization_preview_rejects_queue_id_owned_by_different_book(monkeypatch, tmp_path):
    library = tmp_path / "library.epub"
    staged = tmp_path / "staged.epub"
    library.write_bytes(b"fixture")
    staged.write_bytes(b"fixture")
    expected = "a" * 64
    acquisition = {
        "id": 81,
        "result_id": 91,
        "book_id": 101,
        "status": "cleanup_required",
        "admission_id": 82,
        "staged_relative_path": "Fixture.epub",
        "staged_sha256": expected,
        "queue_id": 77,
        "candidate_title": "Expected Release",
        "candidate_protocol": "usenet",
    }
    admission = {
        "id": 82,
        "result_id": 91,
        "book_id": 101,
        "status": "registered",
        "staged_relative_path": "Fixture.epub",
        "staged_sha256": expected,
        "stored_path": "/data/media/books/Fixture.epub",
        "local_path": str(library),
    }

    class Client:
        def get_book(self, book_id):
            return {"bookFiles": [{"format": "ebook", "path": admission["stored_path"]}]}

    monkeypatch.setattr(finalization, "ebook_acquisition_by_id", lambda acquisition_id: acquisition)
    monkeypatch.setattr(finalization, "ebook_admission_by_id", lambda admission_id: admission)
    monkeypatch.setattr(
        finalization,
        "result_by_id",
        lambda result_id: {
            "stored_path": admission["stored_path"],
            "local_path": admission["local_path"],
        },
    )
    monkeypatch.setattr(
        finalization,
        "_automation_settings",
        lambda: SimpleNamespace(automatic_reacquisition=True),
    )
    monkeypatch.setattr(finalization.settings, "allow_actions", True)
    monkeypatch.setattr(finalization, "sha256_file", lambda path: expected)
    monkeypatch.setattr(finalization, "resolve_staged_file", lambda relative: (tmp_path, staged))
    monkeypatch.setattr(
        finalization,
        "_queue_payload",
        lambda client: ([{
            "id": 77,
            "bookId": 999,
            "title": "Expected Release",
            "protocol": "usenet",
            "status": "imported",
        }], False),
    )

    preview = finalization.finalization_preview(81, Client())

    assert preview["safe"] is False
    assert preview["checks"]["queueIdentityCurrent"] is False
