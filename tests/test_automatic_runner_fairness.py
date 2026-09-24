from types import SimpleNamespace

from app import automatic_runner as runner


def _plan(plan_id, kind):
    return {"id": plan_id, "planKind": kind, "state": "ready"}


def test_waiting_handoff_does_not_starve_later_finalization(monkeypatch):
    plans = [_plan(3, "RECONCILE_ACQUISITION"), _plan(4, "FINALIZE_ACQUISITION")]
    calls = []
    monkeypatch.setattr(runner, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": plans})
    monkeypatch.setattr(runner, "run_acquisition_progress_cycle",
                        lambda plan: calls.append(("wait", plan["id"]))
                        or {"state": "waiting", "externalMutationAttempted": False})
    monkeypatch.setattr(runner, "_run_finalization_cycle",
                        lambda plan: calls.append(("finalize", plan["id"]))
                        or {"state": "executed", "externalMutationAttempted": True})

    result = runner.run_automatic_cycle()

    assert calls == [("wait", 3), ("finalize", 4)]
    assert result["state"] == "executed"


def test_multiple_waiting_handoffs_allow_one_core_item(monkeypatch):
    plans = [
        _plan(3, "RECONCILE_ACQUISITION"),
        _plan(4, "RECONCILE_ACQUISITION"),
        _plan(5, "RETRY_ACQUISITION_TRANSIENT"),
    ]
    calls = []
    monkeypatch.setattr(runner, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": plans})
    monkeypatch.setattr(runner, "run_acquisition_progress_cycle",
                        lambda plan: calls.append(("wait", plan["id"]))
                        or {"state": "waiting", "externalMutationAttempted": False})
    monkeypatch.setattr(runner.core, "run_automatic_cycle",
                        lambda limit: calls.append(("core", limit))
                        or {"state": "executed", "externalMutationAttempted": True})

    result = runner.run_automatic_cycle(limit=10)

    assert calls == [("wait", 3), ("wait", 4), ("core", 10)]
    assert result["state"] == "executed"


def test_all_waiting_returns_oldest_without_mutation(monkeypatch):
    plans = [_plan(3, "RECONCILE_ACQUISITION"), _plan(4, "RECONCILE_ACQUISITION")]
    monkeypatch.setattr(runner, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": plans})
    monkeypatch.setattr(runner, "run_acquisition_progress_cycle",
                        lambda plan: {
                            "state": "waiting",
                            "plan": plan,
                            "externalMutationAttempted": False,
                        })
    monkeypatch.setattr(runner.core, "run_automatic_cycle",
                        lambda limit: (_ for _ in ()).throw(
                            AssertionError("No core work should run")))

    result = runner.run_automatic_cycle()

    assert result["plan"]["id"] == 3
    assert result["externalMutationAttempted"] is False


def test_retry_backoff_does_not_block_later_finalization(monkeypatch):
    plans = [
        {**_plan(3, "RETRY_ACQUISITION_TRANSIENT"), "state": "retry_wait"},
        _plan(4, "FINALIZE_ACQUISITION"),
    ]
    monkeypatch.setattr(runner, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": plans})
    monkeypatch.setattr(runner.core, "run_automatic_cycle",
                        lambda limit: (_ for _ in ()).throw(
                            AssertionError("Waiting retry must not claim the slot")))
    monkeypatch.setattr(runner, "_run_finalization_cycle",
                        lambda plan: {"state": "executed", "plan": plan,
                                      "externalMutationAttempted": True})

    result = runner.run_automatic_cycle()

    assert result["state"] == "executed"
    assert result["plan"]["id"] == 4


def test_only_waiting_retry_still_checks_if_it_became_due(monkeypatch):
    waiting = {**_plan(3, "RETRY_ACQUISITION_TRANSIENT"),
               "state": "retry_wait"}
    calls = []
    monkeypatch.setattr(runner, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": [waiting]})
    monkeypatch.setattr(runner.core, "run_automatic_cycle",
                        lambda limit: calls.append(limit)
                        or {"state": "ready", "plan": waiting})

    result = runner.run_automatic_cycle(limit=10)

    assert calls == [10]
    assert result["state"] == "ready"
