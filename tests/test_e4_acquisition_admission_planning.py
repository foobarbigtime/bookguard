from __future__ import annotations

import pytest

from app.config import settings
from app.db import (
    add_result, create_ebook_acquisition, create_scan, finish_scan,
    init_local_db, local_conn, update_ebook_acquisition,
)
from app.observe import _acquisition_decisions
from app.recovery_planner import _build_plan


STAGED = "Review Fixture.epub"
DIGEST = "a" * 64


def _review(tmp_path, change: str = ""):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    try:
        init_local_db()
        create_scan("admission-review-scan", 1)
        add_result("admission-review-scan", {
            "file_id": 501, "book_id": 101, "author": "Fixture Author",
            "title": "Review Fixture", "format": "ebook",
            "stored_path": "/data/media/books/Review Fixture.epub",
            "local_path": "/books/Review Fixture.epub",
            "classification": "REVIEW", "risk_score": 50,
            "reason_code": "REVIEW_FIXTURE", "reasons": ["fixture"], "metadata": {},
        })
        finish_scan("admission-review-scan")
        with local_conn() as conn:
            result = dict(conn.execute(
                "SELECT * FROM scan_results WHERE scan_id='admission-review-scan'"
            ).fetchone())
        acquisition_id = create_ebook_acquisition(result, {
            "guid": "review-guid", "title": "Review Fixture release",
            "indexerName": "Fixture", "protocol": "usenet",
        })
        verification = {
            "safeToAdmit": True, "stableDuringVerification": True,
            "verdict": "VERIFIED_CORRECT", "bookId": 101,
            "relativePath": STAGED, "size": 123, "sha256": DIGEST,
            "confidence": 100, "admissionBlockers": [],
        }
        if change == "wrong_book":
            verification["bookId"] = 102
        elif change == "wrong_hash":
            verification["sha256"] = "b" * 64
        elif change == "unsafe":
            verification["safeToAdmit"] = False
        elif change == "low_confidence":
            verification["confidence"] = 70
        elif change == "missing_blockers":
            verification.pop("admissionBlockers")
        update_ebook_acquisition(
            acquisition_id, "verified",
            queue_id=None if change == "missing_queue_id" else 77,
            queue_status="downloading" if change == "queue_downloading" else "importexternal",
            staged_relative_path=STAGED, staged_sha256=DIGEST,
            verification=verification,
        )
        with local_conn() as conn:
            decision = next(
                item for item in _acquisition_decisions(conn, 100)
                if int(item["subjectId"]) == acquisition_id
            )
            plan = _build_plan(conn, decision)
        return decision, plan
    finally:
        settings.config_dir = original


def test_exact_durable_verified_acquisition_has_inert_admission_review(tmp_path):
    decision, plan = _review(tmp_path)

    assert decision["decision"] == "would_review_verified_acquisition"
    assert decision["evidence"]["verifiedSnapshotMatches"] is True
    assert decision["evidence"]["stagedSha256"] == DIGEST
    assert plan["planKind"] == "PREPARE_ACQUISITION_ADMISSION"
    assert plan["executionAllowed"] is False
    assert [step["code"] for step in plan["steps"]] == [
        "review_verified_acquisition", "admit_verified_acquisition",
    ]
    assert plan["steps"][0]["externalMutation"] is False
    assert plan["steps"][1]["externalMutation"] is True


@pytest.mark.parametrize("change", [
    "wrong_book", "wrong_hash", "unsafe", "low_confidence",
    "missing_blockers", "missing_queue_id", "queue_downloading",
])
def test_unproven_verified_acquisition_stays_in_attention(tmp_path, change):
    decision, plan = _review(tmp_path, change)

    assert decision["decision"] == "attention"
    assert decision["reasonCode"] == "ACQUISITION_VERIFICATION_UNPROVEN"
    assert decision["evidence"]["verifiedSnapshotMatches"] is False
    assert plan is None
