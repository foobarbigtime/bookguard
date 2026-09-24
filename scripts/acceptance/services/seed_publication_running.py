#!/usr/bin/env python3
"""Simulate a crash after disposable bytes were published but before journal success."""

from pathlib import Path

from app import automatic_execution as execution
from app.recovery_planner import recovery_plan_snapshot


plans = [
    item for item in recovery_plan_snapshot(100)["items"]
    if item["planKind"] == "RECOVER_ADMISSION_PUBLICATION"
    and item["currentStep"] == 4
]
assert len(plans) == 1, "Expected one proved disposable admission plan"
plan = plans[0]
proof = execution._existing(plan["signature"], "prove_supported_no_replace_method", 3)
assert proof and proof["state"] == "succeeded", "Missing completed filesystem proof"
boundary = proof["boundary"]
assert boundary.get("parentDevice") is not None
assert boundary.get("parentInode") is not None

source = Path("/staging/BookGuard Test/Conflict Fixture.epub")
destination = Path("/admission-books/BookGuard Test/Conflict Fixture.epub")
assert not destination.exists(), "Disposable destination must start empty"
destination.write_bytes(source.read_bytes())
execution._record(
    plan, "retry_guarded_publication", 4, "running", boundary=boundary,
    increment_attempt=True,
)
print("Seeded interrupted disposable publication with exact matching bytes.")
