#!/usr/bin/env python3
from __future__ import annotations

import json

from app import automatic_execution
from app.db import update_ebook_admission
from app.recovery_planner import (
    record_recovery_step_success,
    recovery_plan_snapshot,
)


def main() -> None:
    snapshot = recovery_plan_snapshot(100)
    matches = [
        item
        for item in snapshot["items"]
        if item.get("planKind") == "CORRECT_REGISTRATION_CONFLICT"
        and item.get("state") in {"planned", "ready"}
    ]
    if len(matches) != 1:
        raise SystemExit(f"expected one registration correction plan, found {len(matches)}")

    plan = matches[0]
    plan_id = int(plan["id"])
    if int(plan.get("currentStep") or 0) == 0:
        plan = record_recovery_step_success(plan_id, 0)
    if int(plan.get("currentStep") or 0) != 1:
        raise SystemExit(f"expected correction step index 1, got {plan.get('currentStep')}")

    admission_id = int(plan["subjectId"])
    update_ebook_admission(
        admission_id,
        "registration_correcting",
        error="synthetic acceptance crash after correction began",
    )
    execution = automatic_execution._record(
        plan,
        "correct_exact_registration_owner",
        1,
        "running",
        boundary={
            "ok": True,
            "planSignature": str(plan["signature"]),
            "evidenceRevision": str(plan["evidenceRevision"]),
            "checks": [{"code": "SYNTHETIC_ACCEPTANCE_BOUNDARY", "ok": True}],
        },
        increment_attempt=True,
    )
    print(
        json.dumps(
            {
                "planId": plan_id,
                "admissionId": admission_id,
                "executionId": execution["id"],
                "state": execution["state"],
                "attemptCount": execution["attemptCount"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
