#!/usr/bin/env python3
"""Exercise a paused earlier E4 handoff before a real disposable quarantine."""

import json

from app import automatic_runner as runner


original_snapshot = runner.core.recovery_plan_snapshot
calls = {"snapshot": 0, "paused": 0}


def snapshot(limit):
    calls["snapshot"] += 1
    actual = original_snapshot(limit)
    if calls["snapshot"] == 1:
        return {
            **actual,
            "items": [
                {"id": 0, "planKind": "RECOVER_ADMISSION_PUBLICATION",
                 "state": "ready", "currentStep": 0},
                *actual["items"],
            ],
        }
    return actual


def paused_handoff(plan):
    assert plan["id"] == 0
    calls["paused"] += 1
    return {"ok": True, "state": "paused", "plan": plan,
            "externalMutationAttempted": False}


runner.core.recovery_plan_snapshot = snapshot
runner.run_publication_recovery_cycle = paused_handoff
result = runner.run_automatic_cycle()
assert calls["paused"] == 1
assert result["state"] == "executed"
assert result["plan"]["planKind"] == "QUARANTINE_UNSAFE_MEDIA"
assert result["externalMutationAttempted"] is True
print(json.dumps({
    "pausedHandoffs": calls["paused"],
    "state": result["state"],
    "planKind": result["plan"]["planKind"],
    "externalMutationAttempted": result["externalMutationAttempted"],
}))
