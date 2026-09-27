"""Read-only final proof for a quarantined ebook's completed replacement."""

from __future__ import annotations

import os
from typing import Any

from . import automatic_execution as core
from .acquisition import AcquisitionSafetyError, _queue_payload
from .admission import AdmissionSafetyError, _exact_ebook_associations, _verified_published_destination
from .bindery_client import BinderyClient, BinderyClientError
from .config import load_automation_settings
from .db import (
    bindery_file_by_id, ebook_admission_by_id, ebook_replacement_for_quarantine_plan,
    result_by_id,
)
from .quarantine_replacement import _current_plan, quarantine_receipt_bytes_match
from .quarantine_selection import quarantine_selection_by_result
from .recovery_planner import recovery_plan_by_id
from .staging import StagingSafetyError, staged_path_is_absent


def quarantine_final_state_preview(plan_id: int) -> dict[str, Any]:
    """Prove the original unsafe bytes remain isolated and the child is finalized."""
    plan = recovery_plan_by_id(int(plan_id))
    if not plan:
        raise ValueError("Quarantine recovery plan not found.")
    result_id = int(plan.get("subjectId") or 0) if plan.get("subjectKind") == "result" else 0
    result = result_by_id(result_id) if result_id else None
    current = _current_plan(result_id) if result else None
    signature = str(plan.get("signature") or "")
    receipt = core._existing(signature, "quarantine_exact_media", 2)
    grab = core._existing(signature, "reacquire_expected_media", 3)
    outcome = (receipt or {}).get("externalResult") or {}
    boundary = (receipt or {}).get("boundary") or {}
    grabbed = (grab or {}).get("externalResult") or {}
    child = ebook_replacement_for_quarantine_plan(int(plan_id))
    selection = quarantine_selection_by_result(result_id) if result_id else None
    admission = ebook_admission_by_id(int(child["admission_id"])) if child and child.get("admission_id") else None
    steps = plan.get("steps") or []

    published = False
    if admission and child and child.get("staged_sha256"):
        try:
            published = _verified_published_destination(
                admission, load_automation_settings()
            ).is_file()
        except (AdmissionSafetyError, OSError, ValueError):
            pass

    staged_absent = False
    if child and child.get("staged_relative_path"):
        try:
            staged_absent = staged_path_is_absent(child["staged_relative_path"])
        except (StagingSafetyError, OSError, ValueError):
            pass

    queue_clear = False
    registered = False
    exact_owner = False
    old_association_absent = False
    if child and admission and result:
        try:
            client = BinderyClient()
            queue, partial = _queue_payload(client)
            queue_clear = bool(
                not partial and child.get("queue_id") is not None
                and not any(str(item.get("id") or "") == str(child["queue_id"]) for item in queue)
            )
            book = client.get_book(int(child["book_id"]))
            expected_path = os.path.normpath(str(admission["stored_path"]))
            registered = any(
                isinstance(item, dict)
                and str(item.get("format") or "").casefold() == "ebook"
                and os.path.normpath(str(item.get("path") or "")) == expected_path
                for item in (book.get("bookFiles") or [])
            )
            owners = _exact_ebook_associations(str(admission["stored_path"]))
            exact_owner = bool(
                len(owners) == 1
                and int(owners[0].get("book_id") or 0) == int(child["book_id"])
                and int(owners[0].get("file_id") or 0) != int(result["file_id"])
            )
            old_association_absent = bindery_file_by_id(int(result["file_id"])) is None
        except (AcquisitionSafetyError, BinderyClientError, OSError, ValueError):
            pass

    checks = [
        {"code": "EXACT_FINAL_STEP", "ok": bool(
            plan.get("planKind") == "QUARANTINE_UNSAFE_MEDIA"
            and plan.get("reasonCode") == "UNSAFE_FILE"
            and plan.get("state") == "ready"
            and int(plan.get("currentStep") or 0) == 5
            and len(steps) == 6
            and steps[2].get("code") == "quarantine_exact_media"
            and steps[3].get("code") == "reacquire_expected_media"
            and steps[4].get("code") == "verify_replacement"
            and steps[5].get("code") == "reconcile_final_state"
        )},
        {"code": "CURRENT_PLAN", "ok": bool(
            result and current and current.get("signature") == signature
            and current.get("evidenceRevision") == plan.get("evidenceRevision")
            and result_id == plan.get("resultId")
            and int(result["book_id"]) == plan.get("bookId")
            and result.get("stored_path") == plan.get("path")
        )},
        {"code": "RETAINED_UNSAFE_BYTES", "ok": bool(
            result and receipt and receipt.get("state") == "succeeded"
            and receipt.get("planId") == int(plan_id)
            and receipt.get("attemptCount") == 1
            and outcome.get("status") == "quarantined"
            and outcome.get("binderyDetached") is True
            and outcome.get("permanentDeletion") is False
            and outcome.get("replacementRequested") is False
            and outcome.get("resultId") == result_id
            and outcome.get("fileId") == int(result["file_id"])
            and outcome.get("bookId") == int(result["book_id"])
            and boundary.get("expectedSha256") == outcome.get("sha256")
            and boundary.get("fileId") == int(result["file_id"])
            and boundary.get("bookId") == int(result["book_id"])
            and boundary.get("storedPath") == result["stored_path"]
            and boundary.get("localPath") == result["local_path"]
            and quarantine_receipt_bytes_match(int(result["book_id"]), outcome)
        )},
        {"code": "EXACT_REPLACEMENT_LINEAGE", "ok": bool(
            result and receipt and selection and child and grab
            and grab.get("state") == "succeeded"
            and grab.get("planId") == int(plan_id)
            and grab.get("attemptCount") == 1
            and selection.get("planId") == int(plan_id)
            and selection.get("planSignature") == signature
            and selection.get("evidenceRevision") == plan.get("evidenceRevision")
            and selection.get("quarantineExecutionId") == receipt.get("id")
            and selection.get("quarantineSha256") == outcome.get("sha256")
            and selection.get("candidateFingerprint") == (grab.get("boundary") or {}).get("candidateFingerprint")
            and grabbed.get("replacementAcquisitionId") == child["id"]
            and grabbed.get("queueId") == child.get("queue_id")
            and grabbed.get("admissionAttempted") is False
            and child.get("result_id") == result_id
            and child.get("book_id") == result["book_id"]
            and child.get("scan_id") == result["scan_id"]
            and bool(child.get("candidate_guid"))
            and child.get("candidate_guid") == (selection.get("candidate") or {}).get("guid")
        )},
        {"code": "FINALIZED_REGISTERED_CHILD", "ok": bool(
            result and child and admission and child.get("status") == "finalized"
            and admission.get("status") == "registered"
            and int(admission.get("id") or 0) == child.get("admission_id")
            and admission.get("result_id") == result_id
            and admission.get("book_id") == child.get("book_id")
            and admission.get("scan_id") == child.get("scan_id")
            and admission.get("stored_path") == result["stored_path"]
            and admission.get("local_path") == result["local_path"]
            and admission.get("staged_relative_path") == child.get("staged_relative_path")
            and admission.get("staged_sha256") == child.get("staged_sha256")
            and len(str(child.get("staged_sha256") or "")) == 64
            and child.get("staged_sha256") != outcome.get("sha256")
        )},
        {"code": "CURRENT_PUBLISHED_BYTES", "ok": published},
        {"code": "EXACT_BINDERY_OWNER", "ok": registered and exact_owner and old_association_absent},
        {"code": "HANDOFF_CLEANED", "ok": queue_clear and staged_absent},
    ]
    return {
        "planId": int(plan_id),
        "replacementAcquisitionId": child.get("id") if child else None,
        "admissionId": admission.get("id") if admission else None,
        "safeToRecord": all(check["ok"] for check in checks),
        "externalMutationAttempted": False,
        "checks": checks,
    }


def run_quarantine_final_state_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    """Complete only the local parent journal after two identical fresh proofs."""
    plan_id = int(plan["id"])
    try:
        proof = quarantine_final_state_preview(plan_id)
        failed = [check["code"] for check in proof["checks"] if not check["ok"]]
        if failed:
            raise ValueError("Final replacement state is unproven: " + ", ".join(failed))
        if quarantine_final_state_preview(plan_id) != proof:
            raise ValueError("Final replacement state changed during proof.")
        updated = core.record_recovery_step_success(plan_id, 5)
    except Exception as exc:
        blocked = core.block_recovery_plan(plan_id, str(exc))
        return {
            "ok": False, "state": "blocked", "plan": blocked,
            "externalMutationAttempted": False, "reasonCode": "FINAL_STATE_UNPROVEN",
            "message": str(exc),
        }
    return {
        "ok": True, "state": "completed", "plan": updated,
        "proof": proof, "externalMutationAttempted": False,
    }
