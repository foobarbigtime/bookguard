from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import automatic_execution as execution
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
