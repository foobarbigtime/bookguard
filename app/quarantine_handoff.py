"""Proof-only completion of a quarantined replacement's verification step."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import acquisition as workflow
from . import automatic_execution as core
from .acquisition_progress import _queue_identity, _verified_snapshot_matches
from .bindery_client import BinderyClient
from .db import associations_inside_path, ebook_replacement_for_quarantine_plan, result_by_id
from .file_safety import sha256_file
from .quarantine_replacement import _current_plan, quarantine_receipt_bytes_match
from .quarantine_selection import quarantine_selection_by_result
from .recovery_planner import recovery_plan_by_id
from .staging import StagingSafetyError, list_staged_ebooks, resolve_staged_file


def quarantine_handoff_preview(plan_id: int) -> dict[str, Any]:
    """Independently prove exact custody, queue and verified staged bytes."""
    plan = recovery_plan_by_id(int(plan_id))
    if not plan:
        raise ValueError("Quarantine recovery plan not found.")
    result_id = int(plan.get("subjectId") or 0) if plan.get("subjectKind") == "result" else 0
    result = result_by_id(result_id) if result_id else None
    child = ebook_replacement_for_quarantine_plan(int(plan_id))
    selection = quarantine_selection_by_result(result_id) if result_id else None
    signature = str(plan.get("signature") or "")
    quarantine = core._existing(signature, "quarantine_exact_media", 2)
    grab = core._existing(signature, "reacquire_expected_media", 3)
    outcome = (quarantine or {}).get("externalResult") or {}
    quarantine_boundary = (quarantine or {}).get("boundary") or {}
    grab_boundary = (grab or {}).get("boundary") or {}
    grabbed = (grab or {}).get("externalResult") or {}
    steps = plan.get("steps") or []

    current = _current_plan(result_id) if result else None
    original = Path(str((result or {}).get("local_path") or "")) if result else None
    source_absent = bool(original and original.is_absolute()
                         and not original.exists() and not original.is_symlink())
    try:
        detached = bool(result and not associations_inside_path(
            str(result["stored_path"])
        ))
        if result:
            workflow._result_and_book(result, BinderyClient())
        book_unoccupied = bool(result)
    except (workflow.AcquisitionSafetyError, OSError, ValueError):
        detached = False
        book_unoccupied = False

    queue, exact_queue, no_competing, queue_complete = {}, False, False, False
    try:
        if child:
            items, partial = workflow._queue_payload(BinderyClient())
            queue, exact_queue, no_competing = _queue_identity(child, items)
            queue_complete = not partial
    except workflow.AcquisitionSafetyError:
        pass

    fingerprint = None
    staged_hash = ""
    try:
        inventory = list_staged_ebooks(1000)
        staged = inventory.get("items") or []
        if not inventory.get("truncated") and len(staged) == 1:
            fingerprint = (
                str(staged[0]["relativePath"]), int(staged[0]["size"]),
                int(staged[0]["modifiedNs"]),
            )
            _, staged_path = resolve_staged_file(fingerprint[0])
            staged_hash = sha256_file(staged_path)
    except (StagingSafetyError, OSError, ValueError):
        pass

    checks = [
        {"code": "EXACT_VERIFICATION_STEP", "ok": bool(
            plan.get("planKind") == "QUARANTINE_UNSAFE_MEDIA"
            and plan.get("reasonCode") == "UNSAFE_FILE"
            and plan.get("subjectKind") == "result"
            and plan.get("state") == "ready"
            and int(plan.get("currentStep") or 0) == 4
            and len(steps) > 4
            and steps[2].get("code") == "quarantine_exact_media"
            and steps[3].get("code") == "reacquire_expected_media"
            and steps[4].get("code") == "verify_replacement"
        )},
        {"code": "CURRENT_RESULT_AND_PLAN", "ok": bool(
            result and current and current.get("signature") == signature
            and current.get("evidenceRevision") == plan.get("evidenceRevision")
            and result_id == plan.get("resultId")
            and int(result["book_id"]) == plan.get("bookId")
            and result.get("stored_path") == plan.get("path")
        )},
        {"code": "EXACT_QUARANTINE_RECEIPT", "ok": bool(
            quarantine and quarantine.get("state") == "succeeded"
            and quarantine.get("planId") == plan_id
            and quarantine.get("attemptCount") == 1
            and outcome.get("status") == "quarantined"
            and outcome.get("binderyDetached") is True
            and outcome.get("permanentDeletion") is False
            and outcome.get("replacementRequested") is False
            and outcome.get("resultId") == result_id
            and result and outcome.get("fileId") == int(result["file_id"])
            and outcome.get("bookId") == int(result["book_id"])
            and quarantine_boundary.get("expectedSha256") == outcome.get("sha256")
            and quarantine_boundary.get("fileId") == int(result["file_id"])
            and quarantine_boundary.get("bookId") == int(result["book_id"])
            and quarantine_boundary.get("storedPath") == result["stored_path"]
            and quarantine_boundary.get("localPath") == result["local_path"]
        )},
        {"code": "RETAINED_QUARANTINE_BYTES", "ok": bool(
            result and quarantine_receipt_bytes_match(int(result["book_id"]), outcome)
        )},
        {"code": "ORIGINAL_DETACHED_AND_BOOK_EMPTY", "ok": bool(
            source_absent and detached and book_unoccupied
        )},
        {"code": "EXACT_OPERATOR_CHOICE_AND_GRAB", "ok": bool(
            selection and grab and grab.get("state") == "succeeded"
            and grab.get("planId") == plan_id and grab.get("attemptCount") == 1
            and selection["planId"] == plan_id
            and selection["planSignature"] == signature
            and selection["evidenceRevision"] == plan.get("evidenceRevision")
            and selection["quarantineExecutionId"] == quarantine.get("id")
            and selection["quarantineSha256"] == outcome.get("sha256")
            and selection["candidateFingerprint"] == grab_boundary.get("candidateFingerprint")
            and selection["quarantineExecutionId"] == grab_boundary.get("quarantineExecutionId")
            and selection["quarantineSha256"] == grab_boundary.get("quarantineSha256")
            and grabbed.get("admissionAttempted") is False
        )},
        {"code": "UNIQUE_VERIFIED_CHILD", "ok": bool(
            child and result and selection
            and child["status"] == "verified" and child["admission_id"] is None
            and int(child["result_id"]) == result_id
            and int(child["book_id"]) == int(result["book_id"])
            and child["scan_id"] == result["scan_id"]
            and child["candidate_guid"] == selection["candidate"]["guid"]
            and child["candidate_title"] == selection["candidate"]["title"]
            and str(child["candidate_protocol"] or "") == selection["candidate"]["protocol"]
            and grabbed.get("replacementAcquisitionId") == child["id"]
            and isinstance(child.get("queue_id"), int)
            and grabbed.get("queueId") == child["queue_id"]
        )},
        {"code": "EXACT_COMPLETED_QUEUE", "ok": bool(
            queue_complete and exact_queue and no_competing
            and workflow._queue_status(queue) in workflow._AWAITING_STAGING_QUEUE_STATUSES
        )},
        {"code": "CURRENT_VERIFIED_STAGING", "ok": bool(
            child and fingerprint and staged_hash
            and child.get("staged_relative_path") == fingerprint[0]
            and child.get("staged_sha256") == staged_hash
            and child.get("observed_relative_path") == fingerprint[0]
            and child.get("observed_size") == fingerprint[1]
            and child.get("observed_modified_ns") == fingerprint[2]
            and _verified_snapshot_matches(
                child, int(plan.get("bookId") or 0), fingerprint, staged_hash,
            )
        )},
    ]
    return {
        "planId": int(plan_id), "resultId": result_id,
        "replacementAcquisitionId": int(child["id"]) if child else None,
        "queueId": child.get("queue_id") if child else None,
        "stagedRelativePath": fingerprint[0] if fingerprint else None,
        "stagedSha256": staged_hash,
        "quarantineSha256": outcome.get("sha256"),
        "safeToRecord": all(check["ok"] for check in checks),
        "externalMutationAttempted": False, "admissionAttempted": False,
        "checks": checks,
    }


def run_quarantine_verification_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = int(plan["id"])
    try:
        proof = quarantine_handoff_preview(plan_id)
        failed = [check["code"] for check in proof["checks"] if not check["ok"]]
        if failed:
            raise ValueError("Replacement handoff is unproven: " + ", ".join(failed))
        repeated = quarantine_handoff_preview(plan_id)
        if not repeated["safeToRecord"] or repeated != proof:
            raise ValueError("Replacement handoff changed during the proof.")
        updated = core.record_recovery_step_success(plan_id, 4)
    except Exception as exc:
        blocked = core.block_recovery_plan(plan_id, str(exc))
        return {
            "ok": False, "state": "blocked", "plan": blocked,
            "externalMutationAttempted": False, "reasonCode": "HANDOFF_PROOF_FAILED",
            "message": str(exc),
        }
    return {
        "ok": True, "state": "verified", "plan": updated,
        "proof": proof, "externalMutationAttempted": False,
        "admissionAttempted": False,
    }
