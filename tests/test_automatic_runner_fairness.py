from types import SimpleNamespace

import pytest

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
                        lambda limit, selected_plan=None: calls.append(
                            ("core", limit, selected_plan["id"]))
                        or {"state": "executed", "externalMutationAttempted": True})

    result = runner.run_automatic_cycle(limit=10)

    assert calls == [("wait", 3), ("wait", 4), ("core", 10, 5)]
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


def test_paused_alternate_parent_does_not_starve_linked_child(monkeypatch):
    parent = _plan(3, "SELECT_ALTERNATE_REPLACEMENT")
    child = _plan(4, "RECONCILE_ACQUISITION")
    calls = []
    monkeypatch.setattr(runner, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": [child, parent]})
    monkeypatch.setattr(runner, "run_alternate_grab_cycle",
                        lambda plan: calls.append(("parent", plan["id"]))
                        or {"state": "paused", "plan": plan,
                            "externalMutationAttempted": False})
    monkeypatch.setattr(runner, "run_acquisition_progress_cycle",
                        lambda plan: calls.append(("child", plan["id"]))
                        or {"state": "waiting", "plan": plan,
                            "externalMutationAttempted": False})

    result = runner.run_automatic_cycle()

    assert calls == [("parent", 3), ("child", 4)]
    assert result["state"] == "waiting"
    assert result["plan"]["id"] == 4


def test_completed_quarantine_does_not_starve_later_finalization(monkeypatch):
    paused = {
        **_plan(3, "QUARANTINE_UNSAFE_MEDIA"),
        "currentStep": 3,
        "steps": [
            {"code": "revalidate_unsafe_verdict"},
            {"code": "capture_exact_source_identity"},
            {"code": "quarantine_exact_media"},
            {"code": "reacquire_expected_media"},
        ],
    }
    ready = _plan(4, "FINALIZE_ACQUISITION")
    calls = []
    monkeypatch.setattr(runner, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": [paused, ready]})
    monkeypatch.setattr(runner, "_run_finalization_cycle",
                        lambda plan: calls.append(plan["id"])
                        or {"state": "executed", "plan": plan,
                            "externalMutationAttempted": True})
    monkeypatch.setattr(runner.core, "run_automatic_cycle",
                        lambda limit: (_ for _ in ()).throw(
                            AssertionError("Paused quarantine claimed the live slot")))

    result = runner.run_automatic_cycle()

    assert calls == [4]
    assert result["plan"]["id"] == 4


def test_only_completed_quarantine_remains_visible_as_paused(monkeypatch):
    paused = {
        **_plan(3, "QUARANTINE_UNSAFE_MEDIA"),
        "currentStep": 3,
        "steps": [{"code": "a"}, {"code": "b"},
                  {"code": "quarantine_exact_media"},
                  {"code": "reacquire_expected_media"}],
    }
    monkeypatch.setattr(runner, "load_automation_settings",
                        lambda: SimpleNamespace(automation_mode="automatic"))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": [paused]})

    result = runner.run_automatic_cycle()

    assert result["state"] == "paused"
    assert result["plan"]["id"] == 3
    assert result["externalMutationAttempted"] is False


@pytest.mark.parametrize(
    ("kind", "runner_name", "allowlist"),
    [
        ("PREPARE_ACQUISITION_ADMISSION", "run_verified_admission_cycle",
         "admit_verified_acquisition"),
        ("REVIEW_ADMISSION_PREPUBLICATION", "run_prepublication_retirement_cycle",
         "retire_proven_prepublication_failure"),
        ("REQUEST_PUBLISHED_ACQUISITION_SCAN", "run_published_acquisition_scan_cycle",
         "request_published_acquisition_scan"),
        ("RECOVER_ADMISSION_PUBLICATION", "run_publication_recovery_cycle", None),
        ("CORRECT_REGISTRATION_CONFLICT", "run_registration_conflict_cycle", None),
        ("FINALIZE_ACQUISITION", "_run_finalization_cycle", None),
    ],
)
def test_paused_no_mutation_plan_yields_to_later_ready_item(
    monkeypatch, kind, runner_name, allowlist,
):
    paused = _plan(3, kind)
    ready = _plan(4, "FINALIZE_ACQUISITION")
    calls = []
    monkeypatch.setattr(runner, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic",
        automatic_action_allowlist=(allowlist,) if allowlist else (),
    ))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": [paused, ready]})
    monkeypatch.setattr(runner, runner_name,
                        lambda plan: calls.append(plan["id"])
                        or {"state": "paused", "plan": plan,
                            "externalMutationAttempted": False})
    if runner_name != "_run_finalization_cycle":
        monkeypatch.setattr(runner, "_run_finalization_cycle",
                            lambda plan: calls.append(plan["id"])
                            or {"state": "executed", "plan": plan,
                                "externalMutationAttempted": True})
    else:
        ready = _plan(4, "RECONCILE_ACQUISITION")
        monkeypatch.setattr(runner, "run_acquisition_progress_cycle",
                            lambda plan: calls.append(plan["id"])
                            or {"state": "executed", "plan": plan,
                                "externalMutationAttempted": True})

    result = runner.run_automatic_cycle()

    assert calls == [3, 4]
    assert result["plan"]["id"] == 4


def test_waiting_after_external_attempt_stops_before_later_mutation(monkeypatch):
    plans = [_plan(3, "PREPARE_ACQUISITION_ADMISSION"),
             _plan(4, "FINALIZE_ACQUISITION")]
    monkeypatch.setattr(runner, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic",
        automatic_action_allowlist=("admit_verified_acquisition",),
    ))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": plans})
    monkeypatch.setattr(runner, "run_verified_admission_cycle",
                        lambda plan: {"state": "waiting", "plan": plan,
                                      "externalMutationAttempted": True})
    monkeypatch.setattr(runner, "_run_finalization_cycle",
                        lambda plan: (_ for _ in ()).throw(
                            AssertionError("A second mutation was attempted")))

    result = runner.run_automatic_cycle()

    assert result["plan"]["id"] == 3
    assert result["externalMutationAttempted"] is True


def test_core_paused_retry_yields_to_later_outer_finalization(monkeypatch):
    retry = _plan(3, "RETRY_ACQUISITION_TRANSIENT")
    ready = _plan(4, "FINALIZE_ACQUISITION")
    calls = []
    monkeypatch.setattr(runner, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic", automatic_action_allowlist=(),
    ))
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": [retry, ready]})
    monkeypatch.setattr(runner.core, "run_automatic_cycle",
                        lambda limit, selected_plan=None: calls.append(
                            ("core", selected_plan["id"])) or {
                            "state": "paused", "plan": retry,
                            "externalMutationAttempted": False,
                        })
    monkeypatch.setattr(runner, "_run_finalization_cycle",
                        lambda plan: calls.append("finalize") or {
                            "state": "executed", "plan": plan,
                            "externalMutationAttempted": True,
                        })

    result = runner.run_automatic_cycle()

    assert calls == [("core", 3), "finalize"]
    assert result["plan"]["id"] == 4
