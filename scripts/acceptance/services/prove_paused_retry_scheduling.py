#!/usr/bin/env python3
"""Exercise a completed retry before a real, separately allowlisted quarantine."""

import json

from app import automatic_execution as core


original_snapshot = core.recovery_plan_snapshot
calls = {"paused": 0}


def snapshot(limit):
    actual = original_snapshot(limit)
    return {
        **actual,
        "items": [
            {
                "id": 0, "planKind": "RETRY_ACQUISITION_TRANSIENT",
                "state": "ready", "currentStep": 3,
                "steps": [
                    {"code": "wait_bounded_backoff"},
                    {"code": "revalidate_acquisition_readiness"},
                    {"code": "retry_grab_once"},
                    {"code": "reconcile_after_retry"},
                ],
            },
            *actual["items"],
        ],
    }


def no_handoff(plan):
    assert plan["id"] == 0
    calls["paused"] += 1
    return None


core.recovery_plan_snapshot = snapshot
core._handoff_completed_retry = no_handoff
result = core.run_automatic_cycle()
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
