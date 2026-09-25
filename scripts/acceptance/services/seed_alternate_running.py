#!/usr/bin/env python3
"""Seed a crash after an alternate grab was accepted, before journal success."""

from app import automatic_execution as execution
from app.acquisition import _search_candidate, _result_and_book
from app.automatic_alternate import _EXECUTOR
from app.bindery_client import BinderyClient
from app.db import (
    create_ebook_acquisition, result_by_id, update_ebook_acquisition,
)
from app.recovery_planner import recovery_plan_snapshot


plans = [
    item for item in recovery_plan_snapshot(100)["items"]
    if item["planKind"] == "SELECT_ALTERNATE_REPLACEMENT"
    and item["currentStep"] == 3
]
assert len(plans) == 1, "Expected one selected alternate plan at the grab step"
plan = plans[0]
boundary = _EXECUTOR.revalidate(plan, plan["steps"][3])
client = BinderyClient()
result = result_by_id(int(plan["resultId"]))
_, title, author = _result_and_book(result, client)
candidate = _search_candidate(
    client, int(plan["bookId"]), boundary["candidateGuid"], title, author,
)
child_id = create_ebook_acquisition(
    result, candidate, replacement_for_acquisition_id=int(plan["subjectId"]),
)
update_ebook_acquisition(child_id, "grab_requested")
execution._record(
    plan, "request_alternate_grab", 3, "running", boundary=boundary,
    increment_attempt=True,
)
print(f"Seeded interrupted disposable alternate child {child_id} without calling grab.")
