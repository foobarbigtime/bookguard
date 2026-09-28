#!/usr/bin/env python3
"""Interrupt a disposable local retirement after its durable running receipt."""

from app import automatic_execution as core
from app.db import local_conn
from app.prepublication_retirement import EXECUTOR
from app.recovery_planner import recovery_plan_by_id


with local_conn() as conn:
    row = conn.execute(
        """SELECT id FROM recovery_plans
           WHERE plan_kind='REVIEW_ADMISSION_PREPUBLICATION'
             AND subject_kind='admission' ORDER BY id DESC LIMIT 1"""
    ).fetchone()
assert row
plan = recovery_plan_by_id(int(row["id"]))
assert plan and plan["planKind"] == "REVIEW_ADMISSION_PREPUBLICATION"
for index in (0, 1):
    plan = core.record_recovery_step_success(int(plan["id"]), index)
step = plan["steps"][2]
boundary = EXECUTOR.revalidate(plan, step)
core._require_fresh_boundary(plan, boundary)
core._record(
    plan, "retire_proven_prepublication_failure", 2, "running",
    boundary=boundary, increment_attempt=True,
)
outcome = EXECUTOR.execute(plan, step, boundary)
assert outcome["status"] == "retired_before_publication"
assert outcome["libraryBytesChanged"] is False
print("Seeded interrupted disposable retirement after local transition; receipt remains running.")
