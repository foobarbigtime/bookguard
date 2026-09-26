#!/usr/bin/env python3
"""Seed a running receipt and exact unscanned published bytes."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from app import automatic_execution as core
from app.acquisition_admission_execution import EXECUTOR
from app.db import (
    create_ebook_admission, ebook_acquisition_by_id, result_by_id,
    update_ebook_admission,
)
from app.recovery_planner import recovery_plan_by_id


def main() -> None:
    plan = recovery_plan_by_id(1)
    assert plan["planKind"] == "PREPARE_ACQUISITION_ADMISSION"
    assert plan["currentStep"] == 0
    plan = core.record_recovery_step_success(int(plan["id"]), 0)
    boundary = EXECUTOR.revalidate(plan, plan["steps"][1])
    core._require_fresh_boundary(plan, boundary)
    core._record(
        plan, "admit_verified_acquisition", 1, "running",
        boundary=boundary, increment_attempt=True,
    )
    acquisition = ebook_acquisition_by_id(int(plan["subjectId"]))
    result = result_by_id(int(plan["resultId"]))
    admission_id = create_ebook_admission(
        result, str(acquisition["staged_relative_path"]),
    )
    source = Path("/staging") / boundary["stagedRelativePath"]
    destination = Path(boundary["destination"])
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
    update_ebook_admission(
        admission_id, "published",
        staged_sha256=boundary["stagedSha256"],
        publication_method="private-snapshot-link",
        verification=acquisition["verification"],
    )
    print(json.dumps({
        "admissionId": admission_id, "sha256": boundary["stagedSha256"],
        "scanRequested": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
