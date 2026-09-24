#!/usr/bin/env python3
"""Simulate interruption before the disposable Bindery scan request outcome."""

from app import automatic_execution as core
from app import publication_registration
from app.recovery_planner import recovery_plan_snapshot


plans = [
    item for item in recovery_plan_snapshot(100)["items"]
    if item["planKind"] == "RECOVER_ADMISSION_PUBLICATION"
    and item["currentStep"] == 5
]
assert len(plans) == 1, "Expected one published disposable admission"
plan = plans[0]
boundary = publication_registration._EXECUTOR.revalidate(plan, plan["steps"][5])
core._require_fresh_boundary(plan, boundary)
core._record(
    plan, "scan_and_reconcile_registration", 5, "running",
    boundary=boundary, increment_attempt=True,
)
print("Seeded a running scan journal before any Bindery scan request.")
