"""Read-only mutation-boundary preview for a verified acquisition."""

from __future__ import annotations

from typing import Any

from .acquisition import (
    AcquisitionSafetyError, _AWAITING_STAGING_QUEUE_STATUSES, _queue_payload,
    _queue_status,
)
from .acquisition_progress import _queue_identity, _verified_snapshot_matches
from .admission import (
    AdmissionSafetyError, _book_has_ebook, _destination_for_result,
    _result_matches_book, admission_readiness,
)
from .bindery_client import BinderyClient, BinderyClientError
from .db import ebook_acquisition_by_id, local_conn, result_by_id
from .file_safety import sha256_file
from .observe import _acquisition_decisions
from .recovery_planner import _build_plan, recovery_plan_by_id
from .staging import (
    StagingSafetyError, list_staged_ebooks, resolve_staged_file,
    verify_staged_ebook,
)


def _blocked(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False, "readOnly": True, "reasonCode": code,
        "message": message, "admissionAttempted": False,
    }


def acquisition_admission_preview(
    acquisition_id: int, client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Recheck current evidence without creating an admission or publishing bytes."""
    acquisition = ebook_acquisition_by_id(acquisition_id)
    if not acquisition or acquisition.get("status") != "verified":
        return _blocked("VERIFIED_ACQUISITION_REQUIRED", "No verified acquisition is current.")
    with local_conn() as conn:
        decision = next((
            item for item in _acquisition_decisions(conn, 2000)
            if item.get("subjectKind") == "acquisition"
            and str(item.get("subjectId")) == str(acquisition_id)
        ), None)
        current = _build_plan(conn, decision) if decision else None
        recorded = conn.execute(
            """SELECT id FROM recovery_plans
               WHERE subject_kind='acquisition' AND subject_id=?
                 AND state IN ('planned', 'ready')
               ORDER BY id DESC LIMIT 1""",
            (str(acquisition_id),),
        ).fetchone()
        existing_admission = conn.execute(
            "SELECT id FROM ebook_admissions WHERE result_id=? LIMIT 1",
            (int(acquisition["result_id"]),),
        ).fetchone()
    plan = recovery_plan_by_id(int(recorded["id"])) if recorded else None
    if (
        not current or not plan
        or current.get("planKind") != "PREPARE_ACQUISITION_ADMISSION"
        or current.get("signature") != plan.get("signature")
        or current.get("evidenceRevision") != plan.get("evidenceRevision")
        or str(plan.get("subjectId")) != str(acquisition_id)
    ):
        return _blocked("CURRENT_PLAN_UNPROVEN", "The exact admission review plan is not current.")
    if existing_admission or acquisition.get("admission_id") is not None:
        return _blocked("ADMISSION_ALREADY_EXISTS", "An admission already exists for this result.")
    if (
        acquisition.get("result_id") != plan.get("resultId")
        or acquisition.get("book_id") != plan.get("bookId")
        or acquisition.get("staged_relative_path") != plan.get("path")
        or acquisition.get("staged_sha256") != (decision.get("evidence") or {}).get("stagedSha256")
    ):
        return _blocked("DURABLE_IDENTITY_CHANGED", "The acquisition differs from its review plan.")

    client = client or BinderyClient()
    try:
        items, partial = _queue_payload(client)
        queue, exact, no_competing = _queue_identity(acquisition, items)
        if partial or not exact or not no_competing or (
            _queue_status(queue) not in _AWAITING_STAGING_QUEUE_STATUSES
        ):
            return _blocked("QUEUE_HANDOFF_UNPROVEN", "The exact completed queue handoff is unproven.")
        inventory = list_staged_ebooks(1000)
        staged = inventory.get("items") or []
        if inventory.get("truncated") or len(staged) != 1:
            return _blocked("STAGING_AMBIGUOUS", "Exactly one staged ebook is required.")
        fingerprint = (
            str(staged[0]["relativePath"]), int(staged[0]["size"]),
            int(staged[0]["modifiedNs"]),
        )
        if fingerprint[0] != acquisition.get("staged_relative_path"):
            return _blocked("STAGING_IDENTITY_CHANGED", "The staged path changed.")
        _, staged_path = resolve_staged_file(fingerprint[0])
        staged_hash = sha256_file(staged_path)
        if (
            staged_hash != acquisition.get("staged_sha256")
            or not _verified_snapshot_matches(
                acquisition, int(plan["bookId"]), fingerprint, staged_hash,
            )
        ):
            return _blocked("STAGED_BYTES_UNPROVEN", "Current bytes differ from durable verification.")
        result = result_by_id(int(plan["resultId"]))
        if not result or result.get("book_id") != plan["bookId"] or result.get("format") != "ebook":
            return _blocked("RESULT_IDENTITY_CHANGED", "The scan result or book changed.")
        book = client.get_book(int(plan["bookId"]))
        if not _result_matches_book(result, book) or _book_has_ebook(book):
            return _blocked("BINDERY_BOOK_CHANGED", "The book identity or registration changed.")
        fresh = verify_staged_ebook(int(plan["bookId"]), fingerprint[0], client)
        if (
            fresh.get("safeToAdmit") is not True
            or fresh.get("sha256") != staged_hash
            or fresh.get("size") != fingerprint[1]
            or fresh.get("relativePath") != fingerprint[0]
        ):
            return _blocked("FRESH_VERIFICATION_FAILED", "Current bytes did not independently verify.")
        if sha256_file(staged_path) != staged_hash:
            return _blocked("STAGED_BYTES_CHANGED", "Staged bytes changed during preview.")
        readiness = admission_readiness(client)
        if not readiness["ready"]:
            return _blocked(
                "ADMISSION_READINESS", "Admission readiness failed: "
                + ", ".join(readiness["blockers"]),
            )
        destination, bindery_path = _destination_for_result(result, staged_path)
        if bindery_path != str(result.get("stored_path") or ""):
            return _blocked("DESTINATION_MAPPING_CHANGED", "The Bindery destination mapping changed.")
    except (
        AcquisitionSafetyError, AdmissionSafetyError, BinderyClientError,
        StagingSafetyError, OSError, ValueError, TypeError,
    ) as exc:
        return _blocked("DEPENDENCY_OR_IDENTITY_UNPROVEN", str(exc))
    return {
        "ok": True, "readOnly": True, "admissionAttempted": False,
        "acquisitionId": acquisition_id, "planSignature": plan["signature"],
        "evidenceRevision": plan["evidenceRevision"],
        "stagedRelativePath": fingerprint[0], "stagedSha256": staged_hash,
        "destination": str(destination), "binderyPath": bindery_path,
        "message": "Current evidence is suitable for review; publication remains disabled.",
    }
