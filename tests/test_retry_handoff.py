from contextlib import nullcontext
from types import SimpleNamespace

from app import automatic_execution as execution


def _retry_plan():
    return {
        "id": 12, "signature": "retry-signature",
        "evidenceRevision": "retry-revision",
        "planKind": "RETRY_ACQUISITION_TRANSIENT",
        "subjectKind": "acquisition", "subjectId": "21",
        "resultId": 8, "bookId": 42, "state": "ready",
        "currentStep": 3,
        "steps": [
            {"code": "wait_bounded_backoff"},
            {"code": "revalidate_acquisition_readiness"},
            {"code": "retry_grab_once"},
            {"code": "reconcile_after_retry"},
        ],
    }


def test_proven_retry_hands_off_to_fresh_staging_plan(monkeypatch):
    plan = _retry_plan()
    acquisition = {
        "status": "queued", "queue_id": 77, "result_id": 8,
        "book_id": 42, "admission_id": None,
    }
    decision = {
        "subjectKind": "acquisition", "subjectId": "21",
        "decision": "would_reconcile_acquisition",
        "reasonCode": "ACQUISITION_PROGRESSABLE",
        "resultId": 8, "bookId": 42, "evidence": {"queueId": 77},
    }
    saved = []
    monkeypatch.setattr(execution, "_existing", lambda *args: {
        "state": "succeeded",
        "externalResult": {"acquisitionId": 21, "queueId": 77},
    })
    monkeypatch.setattr(execution, "ebook_acquisition_by_id", lambda _: acquisition)
    monkeypatch.setattr(execution, "local_conn", lambda: nullcontext(
        SimpleNamespace(commit=lambda: None)))
    monkeypatch.setattr(execution, "_acquisition_decisions", lambda conn, limit: [decision])
    monkeypatch.setattr(execution, "record_recovery_plans",
                        lambda conn, decisions: saved.append(decisions)
                        or [{"id": 13, "planKind": "RECONCILE_ACQUISITION"}])

    result = execution._handoff_completed_retry(plan)

    assert result["state"] == "handed_off"
    assert result["plan"]["id"] == 13
    assert result["externalMutationAttempted"] is False
    assert saved == [[decision]]


def test_unproven_retry_does_not_create_new_authority(monkeypatch):
    plan = _retry_plan()
    monkeypatch.setattr(execution, "_existing", lambda *args: {
        "state": "succeeded",
        "externalResult": {"acquisitionId": 21, "queueId": 999},
    })
    monkeypatch.setattr(execution, "ebook_acquisition_by_id", lambda _: {
        "status": "queued", "queue_id": 77, "result_id": 8,
        "book_id": 42, "admission_id": None,
    })
    monkeypatch.setattr(execution, "record_recovery_plans",
                        lambda *args: (_ for _ in ()).throw(
                            AssertionError("Unproven handoff must not plan")))

    assert execution._handoff_completed_retry(plan) is None


def test_next_cycle_hands_off_instead_of_replaying_grab(monkeypatch):
    plan = _retry_plan()
    monkeypatch.setattr(execution, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(execution, "recovery_plan_snapshot",
                        lambda limit: {"items": [plan]})
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda _: plan)
    monkeypatch.setattr(execution, "_handoff_completed_retry",
                        lambda _: {"state": "handed_off",
                                   "externalMutationAttempted": False})
    monkeypatch.setattr(execution, "attempt_automatic_step",
                        lambda _: (_ for _ in ()).throw(
                            AssertionError("The grab must not repeat")))

    result = execution.run_automatic_cycle()

    assert result["state"] == "handed_off"
    assert result["externalMutationAttempted"] is False


def test_core_skips_waiting_retry_for_ready_quarantine(monkeypatch):
    waiting = {**_retry_plan(), "id": 2, "state": "retry_wait"}
    quarantine = {"id": 3, "state": "ready",
                  "planKind": "QUARANTINE_UNSAFE_MEDIA"}
    monkeypatch.setattr(execution, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(execution, "promote_due_recovery_retries", lambda: [])
    monkeypatch.setattr(execution, "recovery_plan_snapshot",
                        lambda limit: {"items": [waiting, quarantine]})
    monkeypatch.setattr(execution, "_run_unsafe_quarantine_cycle",
                        lambda plan: {"state": "executed", "plan": plan})

    result = execution.run_automatic_cycle()

    assert result["state"] == "executed"
    assert result["plan"]["id"] == 3


def test_due_retry_is_promoted_before_core_selects_work(monkeypatch):
    ready = {**_retry_plan(), "state": "ready", "currentStep": 3}
    monkeypatch.setattr(execution, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(execution, "promote_due_recovery_retries",
                        lambda: [ready])
    monkeypatch.setattr(execution, "recovery_plan_snapshot",
                        lambda limit: {"items": [ready]})
    monkeypatch.setattr(execution, "recovery_plan_by_id", lambda _: ready)
    monkeypatch.setattr(execution, "_handoff_completed_retry",
                        lambda plan: {"state": "handed_off", "plan": plan})

    result = execution.run_automatic_cycle()

    assert result["state"] == "handed_off"
    assert result["plan"]["id"] == 12
