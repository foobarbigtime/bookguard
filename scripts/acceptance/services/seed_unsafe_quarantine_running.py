#!/usr/bin/env python3
"""Leave a durable running receipt before a disposable quarantine move."""

from app import automatic_execution as core
from app.db import local_conn
from app.recovery_planner import recovery_plan_by_id


with local_conn() as conn:
    row = conn.execute(
        """SELECT id FROM recovery_plans
           WHERE plan_kind='QUARANTINE_UNSAFE_MEDIA' AND subject_kind='result'
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()
assert row
plan = recovery_plan_by_id(int(row["id"]))
assert plan and plan["planKind"] == "QUARANTINE_UNSAFE_MEDIA"
for index in (0, 1):
    plan = core.record_recovery_step_success(int(plan["id"]), index)
step = plan["steps"][2]
boundary = core._EXECUTORS["quarantine_exact_media"].revalidate(plan, step)
core._require_fresh_boundary(plan, boundary)
core._record(
    plan, "quarantine_exact_media", 2, "running",
    boundary=boundary, increment_attempt=True,
)
print("Seeded interrupted disposable quarantine before any external mutation.")
