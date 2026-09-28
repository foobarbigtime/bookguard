from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import automatic_execution as execution
from app import automatic_retry as retry
from app import automatic_quarantine as quarantine
from app import automatic_admission as admission_executor
from app.routes import automatic as automatic_routes


def _configured(*, mode="automatic", allowlist=()):
    return SimpleNamespace(
        automation_mode=mode,
        automatic_action_allowlist=tuple(allowlist),
    )


def _plan(step):
    return {
        "id": 7,
        "signature": "plan-signature",
        "state": "ready",
        "evidenceRevision": "evidence-1",
        "currentStep": 0,
        "steps": [step],
    }


def test_execution_policy_is_inert_without_registered_executor(monkeypatch):
    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "_EXECUTORS", {})

    policy = execution.execution_policy_snapshot()

    assert policy["automaticModeActive"] is True
    assert policy["allowlistedActionCodes"] == ["retry_grab_once"]
    assert policy["registeredExecutorCodes"] == []
    assert policy["executableActionCodes"] == []
    assert policy["liveMutationReady"] is False
    assert policy["manualMutationEndpointsAllowed"] is False


def test_automatic_mode_blocks_legacy_manual_mutation_routes(monkeypatch):
    monkeypatch.setattr(
        automatic_routes,
        "load_automation_settings",
        lambda: _configured(),
    )

    with pytest.raises(HTTPException) as exc:
        automatic_routes._require_mutation_mode()

    assert exc.value.status_code == 409
    assert "manual-only" in str(exc.value.detail)


def test_allowlisted_step_without_executor_fails_closed(monkeypatch):
    step = {"code": "retry_grab_once", "externalMutation": True}
    plan = _plan(step)
    recorded = []

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(execution, "_existing", lambda *args: None)
    monkeypatch.setattr(execution, "_EXECUTORS", {})
    monkeypatch.setattr(
        execution,
        "_record",
        lambda *args, **kwargs: recorded.append((args, kwargs)) or {},
    )

    with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
        execution.attempt_automatic_step(7)

    assert exc.value.reason_code == "EXECUTOR_NOT_REGISTERED"
    assert recorded
    assert recorded[0][0][3] == "blocked"


def test_read_only_current_step_never_reaches_executor(monkeypatch):
    plan = _plan({"code": "revalidate_acquisition_readiness", "externalMutation": False})
    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)

    with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
        execution.attempt_automatic_step(7)

    assert exc.value.reason_code == "READ_ONLY_STEP_PENDING"


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        ({}, "BOUNDARY_REVALIDATION_FAILED"),
        (
            {
                "ok": True,
                "planSignature": "different",
                "evidenceRevision": "evidence-1",
                "checks": [{"code": "PATH_UNCHANGED", "ok": True}],
            },
            "PLAN_SIGNATURE_CHANGED",
        ),
        (
            {
                "ok": True,
                "planSignature": "plan-signature",
                "evidenceRevision": "different",
                "checks": [{"code": "PATH_UNCHANGED", "ok": True}],
            },
            "EVIDENCE_REVISION_CHANGED",
        ),
        (
            {
                "ok": True,
                "planSignature": "plan-signature",
                "evidenceRevision": "evidence-1",
                "checks": [],
            },
            "BOUNDARY_CHECKS_MISSING",
        ),
        (
            {
                "ok": True,
                "planSignature": "plan-signature",
                "evidenceRevision": "evidence-1",
                "checks": [{"code": "PATH_UNCHANGED", "ok": False}],
            },
            "BOUNDARY_INVARIANT_FAILED",
        ),
    ],
)
def test_mutation_boundary_fails_closed(monkeypatch, boundary, reason):
    with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
        execution._require_fresh_boundary(_plan({}), boundary)

    assert exc.value.reason_code == reason


def test_registered_executor_revalidates_before_mutation(monkeypatch):
    step = {"code": "retry_grab_once", "externalMutation": True}
    plan = _plan(step)
    calls = []

    class Executor:
        def revalidate(self, current_plan, current_step):
            calls.append("revalidate")
            return {
                "ok": True,
                "planSignature": current_plan["signature"],
                "evidenceRevision": current_plan["evidenceRevision"],
                "checks": [
                    {"code": "SUBJECT_IDENTITY_UNCHANGED", "ok": True},
                    {"code": "EVIDENCE_REVISION_UNCHANGED", "ok": True},
                ],
            }

        def execute(self, current_plan, current_step, boundary):
            calls.append("execute")
            return {"requestId": "test-only"}

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(execution, "_existing", lambda *args: None)
    monkeypatch.setattr(execution, "_EXECUTORS", {"retry_grab_once": Executor()})
    monkeypatch.setattr(
        execution,
        "_record",
        lambda *args, **kwargs: {
            "state": kwargs.get("state") or args[3],
            "actionCode": "retry_grab_once",
        },
    )
    monkeypatch.setattr(
        execution,
        "record_recovery_step_success",
        lambda plan_id, step_index: {"id": plan_id, "currentStep": step_index + 1},
    )

    result = execution.attempt_automatic_step(7)

    assert calls == ["revalidate", "execute"]
    assert result["ok"] is True
    assert result["replayed"] is False
    assert result["plan"]["currentStep"] == 1


def test_automatic_cycle_schedules_first_bounded_retry_without_external_work(monkeypatch):
    plan = {
        "id": 11,
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "state": "planned",
        "currentStep": 0,
        "steps": [{"code": "wait_bounded_backoff", "externalMutation": False}],
    }
    scheduled = {**plan, "state": "retry_wait", "retryCount": 1}

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(mode="automatic", allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(
        execution,
        "recovery_plan_snapshot",
        lambda limit: {"items": [plan]},
    )
    monkeypatch.setattr(
        execution,
        "schedule_recovery_retry",
        lambda plan_id, message: scheduled,
    )
    monkeypatch.setattr(
        execution,
        "attempt_automatic_step",
        lambda plan_id: (_ for _ in ()).throw(AssertionError("external step ran")),
    )

    result = execution.run_automatic_cycle()

    assert result["state"] == "retry_wait"
    assert result["externalMutationAttempted"] is False
    assert result["plan"]["retryCount"] == 1


def test_automatic_cycle_advances_proven_read_only_steps_before_retry(monkeypatch):
    plan0 = {
        "id": 12,
        "signature": "sig",
        "evidenceRevision": "rev",
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "state": "ready",
        "currentStep": 0,
        "steps": [
            {"code": "wait_bounded_backoff", "externalMutation": False},
            {"code": "revalidate_acquisition_readiness", "externalMutation": False},
            {"code": "retry_grab_once", "externalMutation": True},
        ],
    }
    plan1 = {**plan0, "currentStep": 1}
    plan2 = {**plan0, "currentStep": 2}
    calls = []

    class Executor:
        def revalidate(self, plan, step):
            calls.append(("revalidate", plan["currentStep"], step["code"]))
            return {
                "ok": True,
                "planSignature": "sig",
                "evidenceRevision": "rev",
                "checks": [{"code": "CURRENT", "ok": True}],
            }

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(mode="automatic", allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(
        execution,
        "recovery_plan_snapshot",
        lambda limit: {"items": [plan0]},
    )

    def advance(plan_id, step_index):
        calls.append(("advance", step_index))
        return plan1 if step_index == 0 else plan2

    monkeypatch.setattr(execution, "record_recovery_step_success", advance)
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan2)
    monkeypatch.setattr(execution, "_EXECUTORS", {"retry_grab_once": Executor()})
    monkeypatch.setattr(
        execution,
        "attempt_automatic_step",
        lambda plan_id: calls.append(("execute", plan_id))
        or {"ok": True, "replayed": False},
    )

    result = execution.run_automatic_cycle()

    assert calls == [
        ("advance", 0),
        ("revalidate", 1, "revalidate_acquisition_readiness"),
        ("advance", 1),
        ("execute", 12),
    ]
    assert result["state"] == "executed"
    assert result["externalMutationAttempted"] is True


def test_pregrab_read_failure_returns_to_bounded_backoff(monkeypatch):
    plan = {
        "id": 13,
        "subjectId": "21",
        "signature": "sig",
        "evidenceRevision": "rev",
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "state": "ready",
        "currentStep": 2,
        "steps": [
            {"code": "wait_bounded_backoff", "externalMutation": False},
            {"code": "revalidate_acquisition_readiness", "externalMutation": False},
            {"code": "retry_grab_once", "externalMutation": True},
            {"code": "reconcile_after_retry", "externalMutation": True},
        ],
    }
    scheduled = {**plan, "state": "retry_wait", "retryCount": 2}

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(mode="automatic", allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(
        execution,
        "recovery_plan_snapshot",
        lambda limit: {"items": [plan]},
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(
        execution,
        "attempt_automatic_step",
        lambda plan_id: (_ for _ in ()).throw(
            execution.AutomaticExecutionBlocked(
                "EXECUTION_FAILED",
                "Bindery GET /book/21 returned HTTP 503: temporarily unavailable",
            )
        ),
    )
    monkeypatch.setattr(
        execution,
        "ebook_acquisition_by_id",
        lambda acquisition_id: {
            "id": acquisition_id,
            "status": "failed",
            "error": "Bindery GET /book/21 returned HTTP 503: temporarily unavailable",
        },
    )
    monkeypatch.setattr(
        execution,
        "schedule_recovery_retry",
        lambda plan_id, message: scheduled,
    )

    result = execution.run_automatic_cycle()

    assert result["state"] == "retry_wait"
    assert result["externalMutationAttempted"] is True
    assert result["plan"]["retryCount"] == 2


def test_live_retry_failure_blocks_when_classification_changes(monkeypatch):
    plan = {
        "id": 14,
        "subjectId": "22",
        "signature": "sig",
        "evidenceRevision": "rev",
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "state": "ready",
        "currentStep": 2,
        "steps": [
            {"code": "wait_bounded_backoff", "externalMutation": False},
            {"code": "revalidate_acquisition_readiness", "externalMutation": False},
            {"code": "retry_grab_once", "externalMutation": True},
            {"code": "reconcile_after_retry", "externalMutation": True},
        ],
    }
    blocked = {**plan, "state": "blocked"}

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(mode="automatic", allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(
        execution,
        "recovery_plan_snapshot",
        lambda limit: {"items": [plan]},
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(
        execution,
        "attempt_automatic_step",
        lambda plan_id: (_ for _ in ()).throw(
            execution.AutomaticExecutionBlocked(
                "EXECUTION_FAILED",
                "Bindery POST /queue/grab returned HTTP 409: already imported",
            )
        ),
    )
    monkeypatch.setattr(
        execution,
        "ebook_acquisition_by_id",
        lambda acquisition_id: {
            "id": acquisition_id,
            "status": "failed",
            "error": "Bindery POST /queue/grab returned HTTP 409: already imported",
        },
    )
    monkeypatch.setattr(
        execution,
        "schedule_recovery_retry",
        lambda *args: (_ for _ in ()).throw(AssertionError("retry was scheduled")),
    )
    monkeypatch.setattr(
        execution,
        "block_recovery_plan",
        lambda plan_id, message: blocked,
    )

    result = execution.run_automatic_cycle()

    assert result["state"] == "blocked"
    assert result["externalMutationAttempted"] is True
    assert result["reasonCode"] == "RECOVERY_CLASSIFICATION_CHANGED"



def test_running_execution_reconciles_without_reexecuting(monkeypatch):
    step = {"code": "retry_grab_once", "externalMutation": True}
    plan = _plan(step)
    existing = {
        "state": "running",
        "boundary": {
            "ok": True,
            "planSignature": "plan-signature",
            "evidenceRevision": "evidence-1",
            "checks": [{"code": "CURRENT", "ok": True}],
        },
    }
    calls = []

    class Executor:
        def revalidate(self, current_plan, current_step):
            raise AssertionError("normal revalidation must not run")

        def execute(self, current_plan, current_step, boundary):
            raise AssertionError("external mutation must not replay")

        def reconcile_uncertain(self, current_plan, current_step, prior_execution):
            calls.append("reconcile")
            assert prior_execution is existing
            return {
                "acquisitionId": 21,
                "queueId": 77,
                "status": "queued",
                "reconciledAfterRestart": True,
            }

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(execution, "_existing", lambda *args: existing)
    monkeypatch.setattr(execution, "_EXECUTORS", {"retry_grab_once": Executor()})
    monkeypatch.setattr(
        execution,
        "_record",
        lambda *args, **kwargs: {
            "state": kwargs.get("state") or args[3],
            "actionCode": "retry_grab_once",
            "externalResult": kwargs.get("external_result") or {},
        },
    )
    monkeypatch.setattr(
        execution,
        "record_recovery_step_success",
        lambda plan_id, step_index: {"id": plan_id, "currentStep": step_index + 1},
    )

    result = execution.attempt_automatic_step(7)

    assert calls == ["reconcile"]
    assert result["ok"] is True
    assert result["replayed"] is True
    assert result["reconciled"] is True
    assert result["plan"]["currentStep"] == 1
    assert result["execution"]["externalResult"]["queueId"] == 77


def test_running_execution_without_reconciliation_proof_never_replays(monkeypatch):
    step = {"code": "retry_grab_once", "externalMutation": True}
    plan = _plan(step)
    existing = {
        "state": "running",
        "boundary": {
            "ok": True,
            "planSignature": "plan-signature",
            "evidenceRevision": "evidence-1",
            "checks": [{"code": "CURRENT", "ok": True}],
        },
    }
    recorded = []

    class Executor:
        def revalidate(self, current_plan, current_step):
            raise AssertionError("normal revalidation must not run")

        def execute(self, current_plan, current_step, boundary):
            raise AssertionError("external mutation must not replay")

        def reconcile_uncertain(self, current_plan, current_step, prior_execution):
            raise execution.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "remote outcome cannot be proven",
            )

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(execution, "_existing", lambda *args: existing)
    monkeypatch.setattr(execution, "_EXECUTORS", {"retry_grab_once": Executor()})
    monkeypatch.setattr(
        execution,
        "_record",
        lambda *args, **kwargs: recorded.append((args, kwargs)) or {},
    )

    with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
        execution.attempt_automatic_step(7)

    assert exc.value.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME"
    assert recorded
    assert recorded[-1][0][3] == "blocked"


def test_interrupted_retry_queue_proof_adopts_exact_remote_item(monkeypatch):
    plan = {
        "id": 7,
        "subjectId": "21",
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
    }
    acquisition = {
        "id": 21,
        "book_id": 42,
        "candidate_title": "Expected Release",
        "candidate_protocol": "usenet",
        "status": "grab_requested",
    }

    class Client:
        def list_queue(self):
            return {
                "items": [
                    {
                        "id": 77,
                        "bookId": 42,
                        "title": "Expected Release",
                        "protocol": "usenet",
                        "status": "queued",
                    }
                ],
                "partial": False,
            }

    monkeypatch.setattr(retry, "ebook_acquisition_by_id", lambda _id: acquisition)
    monkeypatch.setattr(retry, "BinderyClient", Client)
    monkeypatch.setattr(
        retry,
        "reconcile_ebook_acquisition",
        lambda acquisition_id, client: {
            "ok": True,
            "acquisition": {
                **acquisition,
                "status": "queued",
                "queue_id": 77,
            },
        },
    )

    result = execution._TransientAcquisitionRetryExecutor().reconcile_uncertain(
        plan,
        {"code": "retry_grab_once"},
        {"state": "running"},
    )

    assert result["reconciledAfterRestart"] is True
    assert result["queueId"] == 77
    assert result["status"] == "queued"


def test_interrupted_retry_ambiguous_queue_fails_closed(monkeypatch):
    plan = {
        "id": 7,
        "subjectId": "21",
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
    }
    acquisition = {
        "id": 21,
        "book_id": 42,
        "candidate_title": "Expected Release",
        "candidate_protocol": "usenet",
        "status": "grab_requested",
    }

    class Client:
        def list_queue(self):
            return {
                "items": [
                    {
                        "id": 77,
                        "bookId": 42,
                        "title": "Different Release",
                        "protocol": "usenet",
                        "status": "queued",
                    }
                ],
                "partial": False,
            }

    monkeypatch.setattr(retry, "ebook_acquisition_by_id", lambda _id: acquisition)
    monkeypatch.setattr(retry, "BinderyClient", Client)
    monkeypatch.setattr(
        retry,
        "reconcile_ebook_acquisition",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous queue must not be reconciled")
        ),
    )

    with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
        execution._TransientAcquisitionRetryExecutor().reconcile_uncertain(
            plan,
            {"code": "retry_grab_once"},
            {"state": "running"},
        )

    assert exc.value.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME"



def test_automatic_cycle_reports_reconciliation_without_external_mutation(monkeypatch):
    plan = {
        "id": 15,
        "subjectId": "23",
        "signature": "sig",
        "evidenceRevision": "rev",
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "state": "ready",
        "currentStep": 2,
        "steps": [
            {"code": "wait_bounded_backoff", "externalMutation": False},
            {"code": "revalidate_acquisition_readiness", "externalMutation": False},
            {"code": "retry_grab_once", "externalMutation": True},
            {"code": "reconcile_after_retry", "externalMutation": True},
        ],
    }

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(mode="automatic", allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(
        execution,
        "recovery_plan_snapshot",
        lambda limit: {"items": [plan]},
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(
        execution,
        "attempt_automatic_step",
        lambda plan_id: {
            "ok": True,
            "replayed": True,
            "reconciled": True,
            "execution": {"state": "succeeded"},
            "plan": {**plan, "currentStep": 3},
        },
    )

    result = execution.run_automatic_cycle()

    assert result["state"] == "reconciled"
    assert result["externalMutationAttempted"] is False
    assert result["replayed"] is True
    assert result["reconciled"] is True



def test_unsafe_quarantine_executor_revalidates_exact_current_evidence(monkeypatch):
    plan = {
        "id": 30,
        "signature": "unsafe-signature",
        "evidenceRevision": "unsafe-revision",
        "planKind": "QUARANTINE_UNSAFE_MEDIA",
        "reasonCode": "UNSAFE_FILE",
        "subjectKind": "result",
        "subjectId": "101",
        "resultId": 101,
        "bookId": 202,
        "path": "/data/media/books/Unsafe.epub",
    }
    result = {
        "id": 101,
        "scan_id": "scan-current",
        "file_id": 303,
        "book_id": 202,
        "format": "ebook",
        "stored_path": "/data/media/books/Unsafe.epub",
        "local_path": "/books/Unsafe.epub",
    }
    current_plan = dict(plan)

    class DummyConn:
        pass

    class LocalConn:
        def __enter__(self):
            return DummyConn()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(quarantine, "result_by_id", lambda result_id: result)
    monkeypatch.setattr(
        quarantine,
        "verify_result",
        lambda item, force: {
            "verdict": "UNSAFE_FILE",
            "confidence": 100,
            "source": "deterministic-safety",
        },
    )
    monkeypatch.setattr(quarantine, "local_conn", lambda: LocalConn())
    monkeypatch.setattr(
        quarantine,
        "_result_decisions",
        lambda conn, limit: [{"subjectKind": "result", "subjectId": 101}],
    )
    monkeypatch.setattr(
        quarantine,
        "_build_plan",
        lambda conn, decision: current_plan,
    )
    monkeypatch.setattr(quarantine, "BinderyClient", lambda: object())
    monkeypatch.setattr(
        quarantine,
        "unsafe_media_preview",
        lambda item, client: {
            "safe": True,
            "expectedSha256": "abc123",
            "checks": {
                "exactBinderyAssociation": True,
                "singleAssociation": True,
                "sourceHashMatchesVerification": True,
                "writableAliasReady": True,
                "binderyTracksPath": True,
            },
        },
    )

    boundary = execution._UnsafeMediaQuarantineExecutor().revalidate(
        plan,
        {"code": "quarantine_exact_media"},
    )

    assert boundary["ok"] is True
    assert boundary["expectedSha256"] == "abc123"
    assert all(check["ok"] for check in boundary["checks"])


def test_unsafe_quarantine_cycle_advances_read_only_steps_then_one_mutation(monkeypatch):
    steps = [
        {"code": "revalidate_unsafe_verdict", "externalMutation": False},
        {"code": "capture_exact_source_identity", "externalMutation": False},
        {"code": "quarantine_exact_media", "externalMutation": True},
        {"code": "reacquire_expected_media", "externalMutation": True},
    ]
    plan0 = {
        "id": 31,
        "signature": "sig",
        "evidenceRevision": "rev",
        "planKind": "QUARANTINE_UNSAFE_MEDIA",
        "state": "planned",
        "currentStep": 0,
        "steps": steps,
    }
    current = {"plan": plan0}
    calls = []

    class Executor:
        def revalidate(self, plan, step):
            calls.append(("revalidate", plan["currentStep"], step["code"]))
            return {
                "ok": True,
                "planSignature": "sig",
                "evidenceRevision": "rev",
                "checks": [{"code": "CURRENT", "ok": True}],
            }

    def advance(plan_id, step_index):
        calls.append(("advance", step_index))
        updated = {**current["plan"], "state": "ready", "currentStep": step_index + 1}
        current["plan"] = updated
        return updated

    monkeypatch.setattr(
        execution,
        "_EXECUTORS",
        {"quarantine_exact_media": Executor()},
    )
    monkeypatch.setattr(
        execution,
        "recovery_plan_by_id",
        lambda plan_id: current["plan"],
    )
    monkeypatch.setattr(execution, "record_recovery_step_success", advance)
    monkeypatch.setattr(
        execution,
        "attempt_automatic_step",
        lambda plan_id: calls.append(("execute", plan_id))
        or {"ok": True, "replayed": False},
    )

    result = execution._run_unsafe_quarantine_cycle(plan0)

    assert calls == [
        ("revalidate", 0, "revalidate_unsafe_verdict"),
        ("advance", 0),
        ("revalidate", 1, "capture_exact_source_identity"),
        ("advance", 1),
        ("execute", 31),
    ]
    assert result["state"] == "executed"
    assert result["externalMutationAttempted"] is True


def test_quarantine_exact_media_requires_explicit_allowlist(monkeypatch):
    plan = {
        "id": 32,
        "signature": "sig",
        "state": "ready",
        "evidenceRevision": "rev",
        "currentStep": 0,
        "steps": [{"code": "quarantine_exact_media", "externalMutation": True}],
    }

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(mode="automatic", allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)

    with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
        execution.attempt_automatic_step(32)

    assert exc.value.reason_code == "ACTION_NOT_ALLOWLISTED"



def _admission_plan(*, subject_kind="admission", subject_id="51"):
    return {
        "id": 51,
        "signature": "admission-plan-signature",
        "state": "ready",
        "evidenceRevision": "admission-evidence-1",
        "planKind": "RECONCILE_ADMISSION",
        "reasonCode": "REGISTRATION_SCAN_PENDING",
        "subjectKind": subject_kind,
        "subjectId": subject_id,
        "resultId": 17,
        "bookId": 42,
        "path": "/data/media/books/Bel Canto.epub",
        "currentStep": 1,
        "steps": [
            {"code": "reobserve_registration", "externalMutation": False},
            {"code": "reconcile_known_admission", "externalMutation": True},
        ],
    }


def test_known_admission_executor_revalidates_current_admission_plan(monkeypatch):
    plan = _admission_plan()
    preview = {
        "safe": True,
        "checks": {
            "workflowStateScanRequested": True,
            "publishedBytesCurrent": True,
            "stagedBytesCurrent": True,
            "resultIdentityUnchanged": True,
            "binderyOwnershipConsistent": True,
            "actionsEnabled": True,
            "admissionEnabled": True,
        },
        "admissionId": 51,
        "resultId": 17,
        "bookId": 42,
        "storedPath": "/data/media/books/Bel Canto.epub",
        "stagedSha256": "abc123",
        "registrationState": "scan_required",
    }

    monkeypatch.setattr(
        admission_executor,
        "ebook_admission_by_id",
        lambda admission_id: {"id": admission_id, "status": "scan_requested"},
    )
    monkeypatch.setattr(
        execution._KnownAdmissionReconcileExecutor,
        "_current_plan",
        lambda self, admission_id: dict(plan),
    )
    monkeypatch.setattr(admission_executor, "BinderyClient", lambda: object())
    monkeypatch.setattr(
        admission_executor,
        "admission_reconcile_preview",
        lambda admission_id, client: dict(preview),
    )

    boundary = execution._KnownAdmissionReconcileExecutor().revalidate(
        plan,
        plan["steps"][1],
    )

    assert boundary["ok"] is True
    assert boundary["admissionId"] == 51
    assert boundary["registrationState"] == "scan_required"
    assert all(check["ok"] for check in boundary["checks"])


def test_acquisition_subject_reconcile_admission_is_not_a_live_candidate(monkeypatch):
    acquisition_plan = _admission_plan(subject_kind="acquisition", subject_id="9")
    acquisition_plan["id"] = 9
    admission_plan = _admission_plan(subject_kind="admission", subject_id="51")
    admission_plan["id"] = 51
    calls = []

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(
            mode="automatic",
            allowlist=("reconcile_known_admission",),
        ),
    )
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(
        execution,
        "recovery_plan_snapshot",
        lambda limit: {"items": [acquisition_plan, admission_plan]},
    )
    monkeypatch.setattr(
        execution,
        "_run_admission_reconcile_cycle",
        lambda plan: calls.append(plan["id"]) or {
            "ok": True,
            "state": "executed",
            "externalMutationAttempted": False,
        },
    )

    result = execution.run_automatic_cycle()

    assert calls == [51]
    assert result["state"] == "executed"


def test_core_runner_skips_completed_quarantine_for_independent_ready_item(monkeypatch):
    paused = {
        "id": 3, "planKind": "QUARANTINE_UNSAFE_MEDIA", "state": "ready",
        "currentStep": 3,
        "steps": [{"code": "a"}, {"code": "b"},
                  {"code": "quarantine_exact_media"},
                  {"code": "reacquire_expected_media"}],
    }
    ready = {**_admission_plan(subject_kind="admission", subject_id="51"), "id": 4}
    calls = []
    monkeypatch.setattr(execution, "load_automation_settings",
                        lambda: _configured())
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(execution, "recovery_plan_snapshot",
                        lambda limit: {"items": [paused, ready]})
    monkeypatch.setattr(execution, "_run_admission_reconcile_cycle",
                        lambda plan: calls.append(plan["id"])
                        or {"state": "executed", "plan": plan,
                            "externalMutationAttempted": True})

    result = execution.run_automatic_cycle()

    assert calls == [4]
    assert result["plan"]["id"] == 4


def test_reconcile_known_admission_requires_explicit_allowlist(monkeypatch):
    plan = _admission_plan()

    monkeypatch.setattr(
        execution,
        "load_automation_settings",
        lambda: _configured(mode="automatic", allowlist=("retry_grab_once",)),
    )
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda plan_id: plan)

    with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
        execution.attempt_automatic_step(plan["id"])

    assert exc.value.reason_code == "ACTION_NOT_ALLOWLISTED"


def test_interrupted_admission_scan_is_not_replayed_without_registration_proof(
    monkeypatch,
):
    plan = _admission_plan()
    executor = execution._KnownAdmissionReconcileExecutor()

    monkeypatch.setattr(
        admission_executor,
        "ebook_admission_by_id",
        lambda admission_id: {"id": admission_id, "status": "scan_requested"},
    )
    monkeypatch.setattr(admission_executor, "BinderyClient", lambda: object())
    monkeypatch.setattr(
        admission_executor,
        "admission_reconcile_preview",
        lambda admission_id, client: {
            "safe": True,
            "registrationState": "scan_required",
        },
    )
    monkeypatch.setattr(
        admission_executor,
        "reconcile_admission",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("uncertain scan request must never replay")
        ),
    )

    with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
        executor.reconcile_uncertain(
            plan,
            plan["steps"][1],
            {"state": "running"},
        )

    assert exc.value.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME"


def test_interrupted_admission_scan_adopts_proven_registration_without_scan(
    monkeypatch,
):
    plan = _admission_plan()
    executor = execution._KnownAdmissionReconcileExecutor()
    calls = []

    monkeypatch.setattr(
        admission_executor,
        "ebook_admission_by_id",
        lambda admission_id: {"id": admission_id, "status": "scan_requested"},
    )
    monkeypatch.setattr(admission_executor, "BinderyClient", lambda: object())
    monkeypatch.setattr(
        admission_executor,
        "admission_reconcile_preview",
        lambda admission_id, client: {
            "safe": True,
            "registrationState": "registered",
        },
    )
    monkeypatch.setattr(
        admission_executor,
        "reconcile_admission",
        lambda admission_id, client, *, allow_scan: calls.append(
            (admission_id, allow_scan)) or {
            "admissionId": admission_id,
            "bookId": 42,
            "status": "registered",
            "registered": True,
            "scanRequested": False,
            "stagingRetained": True,
        },
    )

    result = executor.reconcile_uncertain(
        plan,
        plan["steps"][1],
        {"state": "running"},
    )

    assert calls == [(51, False)]
    assert result["status"] == "registered"
    assert result["externalMutationPerformed"] is False
    assert result["reconciledAfterRestart"] is True


def test_uncertain_scan_executor_requires_registration_proof(monkeypatch):
    plan = {**_admission_plan(), "reasonCode": "REGISTRATION_SCAN_OUTCOME_UNKNOWN"}
    preview = {
        "safe": True,
        "scanRequestFailureProven": True,
        "checks": {
            "publishedBytesCurrent": True,
            "stagedBytesCurrent": True,
            "resultIdentityUnchanged": True,
            "binderyOwnershipConsistent": True,
            "actionsEnabled": True,
            "admissionEnabled": True,
        },
        "admissionId": 51, "resultId": 17, "bookId": 42,
        "storedPath": "/data/media/books/Bel Canto.epub",
        "stagedSha256": "abc123", "registrationState": "scan_required",
    }
    monkeypatch.setattr(admission_executor, "ebook_admission_by_id",
                        lambda _: {"id": 51, "status": "scan_request_failed"})
    monkeypatch.setattr(execution._KnownAdmissionReconcileExecutor,
                        "_current_plan", lambda self, _: dict(plan))
    monkeypatch.setattr(admission_executor, "BinderyClient", lambda: object())
    monkeypatch.setattr(admission_executor, "admission_reconcile_preview",
                        lambda *_: dict(preview))
    executor = execution._KnownAdmissionReconcileExecutor()

    boundary = executor.revalidate(plan, plan["steps"][1])
    assert boundary["ok"] is False
    assert any(check["code"] == "UNCERTAIN_SCAN_PROVEN_WITHOUT_RETRY"
               and not check["ok"] for check in boundary["checks"])

    preview["registrationState"] = "registered"
    boundary = executor.revalidate(plan, plan["steps"][1])
    assert boundary["ok"] is True
    calls = []
    monkeypatch.setattr(admission_executor, "reconcile_admission",
                        lambda admission_id, client, *, allow_scan: calls.append(
                            allow_scan) or {
                                "bookId": 42, "status": "registered",
                                "registered": True, "scanRequested": False,
                                "stagingRetained": True,
                            })
    result = executor.execute(plan, plan["steps"][1], boundary)
    assert calls == [False]
    assert result["externalMutationPerformed"] is False


def test_known_admission_execute_never_requests_second_scan(monkeypatch):
    plan = _admission_plan()
    executor = execution._KnownAdmissionReconcileExecutor()

    monkeypatch.setattr(admission_executor, "BinderyClient", lambda: object())
    monkeypatch.setattr(
        admission_executor,
        "reconcile_admission",
        lambda admission_id, client, *, allow_scan: {
            "admissionId": admission_id,
            "bookId": 42,
            "status": "registered",
            "registered": True,
            "scanRequested": False,
            "registrationConflict": None,
            "stagingRetained": True,
        } if allow_scan is False else pytest.fail("automatic reconciliation must not scan"),
    )

    result = executor.execute(plan, plan["steps"][1], {})

    assert result["scanRequested"] is False
    assert result["externalMutationPerformed"] is False
    assert result["libraryBytesChanged"] is False
    assert result["stagingRetained"] is True


def _completed_retry(plan_id):
    return {
        "id": plan_id, "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "state": "ready", "currentStep": 3,
        "steps": [
            {"code": "wait_bounded_backoff"},
            {"code": "revalidate_acquisition_readiness"},
            {"code": "retry_grab_once"},
            {"code": "reconcile_after_retry"},
        ],
    }


def test_completed_retry_without_proof_yields_to_ready_core_item(monkeypatch):
    retry_plan = _completed_retry(3)
    quarantine_plan = {"id": 4, "planKind": "QUARANTINE_UNSAFE_MEDIA",
                       "state": "ready", "currentStep": 2, "steps": []}
    calls = []
    monkeypatch.setattr(execution, "load_automation_settings", lambda: _configured())
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(execution, "recovery_plan_snapshot",
                        lambda limit: {"items": [retry_plan, quarantine_plan]})
    monkeypatch.setattr(execution, "_handoff_completed_retry",
                        lambda plan: calls.append("proof") or None)
    monkeypatch.setattr(execution, "_run_unsafe_quarantine_cycle",
                        lambda plan: calls.append("quarantine") or {
                            "state": "executed", "plan": plan,
                            "externalMutationAttempted": True,
                        })

    result = execution.run_automatic_cycle()

    assert calls == ["proof", "quarantine"]
    assert result["plan"]["id"] == 4


def test_completed_retry_proof_hands_off_before_later_mutation(monkeypatch):
    retry_plan = _completed_retry(3)
    quarantine_plan = {"id": 4, "planKind": "QUARANTINE_UNSAFE_MEDIA",
                       "state": "ready", "currentStep": 2, "steps": []}
    handoff = {"state": "handed_off", "plan": retry_plan,
               "externalMutationAttempted": False}
    monkeypatch.setattr(execution, "load_automation_settings", lambda: _configured())
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(execution, "recovery_plan_snapshot",
                        lambda limit: {"items": [retry_plan, quarantine_plan]})
    monkeypatch.setattr(execution, "_handoff_completed_retry", lambda plan: handoff)
    monkeypatch.setattr(execution, "_run_unsafe_quarantine_cycle",
                        lambda plan: pytest.fail("A proven handoff owns this cycle"))

    assert execution.run_automatic_cycle() is handoff


def test_only_completed_retry_reports_paused(monkeypatch):
    retry_plan = _completed_retry(3)
    monkeypatch.setattr(execution, "load_automation_settings", lambda: _configured())
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(execution, "recovery_plan_snapshot",
                        lambda limit: {"items": [retry_plan]})
    monkeypatch.setattr(execution, "_handoff_completed_retry", lambda plan: None)

    result = execution.run_automatic_cycle()

    assert result["state"] == "paused"
    assert result["plan"]["id"] == 3
    assert result["externalMutationAttempted"] is False


def test_selected_core_plan_runs_without_selecting_again(monkeypatch):
    plan = {"id": 7, "planKind": "QUARANTINE_UNSAFE_MEDIA", "state": "ready"}
    expected = {"state": "executed", "plan": plan,
                "externalMutationAttempted": True}
    monkeypatch.setattr(execution, "load_automation_settings", lambda: _configured())
    monkeypatch.setattr(execution, "recovery_plan_snapshot",
                        lambda limit: pytest.fail("Selected plan was reselected"))
    monkeypatch.setattr(execution, "promote_due_recovery_retries",
                        lambda: pytest.fail("Selected plan was rescheduled"))
    monkeypatch.setattr(execution, "_run_unsafe_quarantine_cycle",
                        lambda chosen: expected if chosen is plan else pytest.fail(
                            "Wrong plan was dispatched"))

    assert execution.run_automatic_cycle(selected_plan=plan) is expected


def test_selected_core_plan_rejects_unsupported_or_waiting_work(monkeypatch):
    monkeypatch.setattr(execution, "load_automation_settings", lambda: _configured())
    for plan in (
        {"id": 7, "planKind": "FINALIZE_ACQUISITION", "state": "ready"},
        {"id": 8, "planKind": "RETRY_ACQUISITION_TRANSIENT", "state": "retry_wait"},
    ):
        with pytest.raises(execution.AutomaticExecutionBlocked) as exc:
            execution.run_automatic_cycle(selected_plan=plan)
        assert exc.value.reason_code == "PLAN_NOT_READY"
