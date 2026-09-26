#!/usr/bin/env python3
"""Create an exact failed BookGuard journal without publishing disposable bytes."""

from app.db import (
    create_ebook_admission, ebook_acquisition_by_id, result_by_id,
    update_ebook_admission,
)


acquisition = ebook_acquisition_by_id(1)
assert acquisition and acquisition["status"] == "verified"
result = result_by_id(int(acquisition["result_id"]))
assert result and acquisition["admission_id"] is None
admission_id = create_ebook_admission(result, acquisition["staged_relative_path"])
update_ebook_admission(
    admission_id, "failed", failure_stage="before_publication",
    error="disposable failure injected before publication",
)
print(f"Seeded disposable prepublication failure {admission_id}; no ebook published.")
