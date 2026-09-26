#!/usr/bin/env python3
"""Seed a durable staging observation after a running progression journal."""

from app import automatic_execution as execution
from app.acquisition_progress import EXECUTOR
from app.db import update_ebook_acquisition
from app.recovery_planner import (
    record_recovery_step_success, recovery_plan_by_id, recovery_plan_snapshot,
)


plans = [
    plan for plan in recovery_plan_snapshot(100)["items"]
    if plan["planKind"] == "RECONCILE_ACQUISITION"
]
assert len(plans) == 1, "Expected one disposable acquisition plan"
plan = record_recovery_step_success(plans[0]["id"], 0)
assert plan["currentStep"] == 1
plan = recovery_plan_by_id(plan["id"])
boundary = EXECUTOR.revalidate(plan, plan["steps"][1])
execution._require_fresh_boundary(plan, boundary)
path, size, modified_ns = boundary["fingerprint"]
execution._record(
    plan, "resume_known_transition", 1, "running", boundary=boundary,
    increment_attempt=True,
)
update_ebook_acquisition(
    int(plan["subjectId"]), "staging_observed",
    observed_relative_path=path, observed_size=size,
    observed_modified_ns=modified_ns,
)
print("Seeded interrupted disposable staging observation without Bindery mutation.")
