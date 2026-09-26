"""Read-only mutation-boundary preview for a verified acquisition."""

from __future__ import annotations

import os
import stat
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
from .file_snapshot import SnapshotError, stable_file_fingerprint
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


def _review_state(acquisition_id: int, result_id: int):
    """Read the current decision, persisted plan, and admission in one DB view."""
    with local_conn() as conn:
        decision = next((
            item for item in _acquisition_decisions(conn, 2000)
            if item.get("subjectKind") == "acquisition"
            and str(item.get("subjectId")) == str(acquisition_id)
        ), None)
        current = _build_plan(conn, decision) if decision else None
        recorded = conn.execute(
            """SELECT id, state, signature, evidence_revision FROM recovery_plans
               WHERE subject_kind='acquisition' AND subject_id=?
                 AND state IN ('planned', 'ready')
               ORDER BY id DESC LIMIT 1""",
            (str(acquisition_id),),
        ).fetchone()
        admission = conn.execute(
            "SELECT id FROM ebook_admissions WHERE result_id=? LIMIT 1",
            (int(result_id),),
        ).fetchone()
    return decision, current, dict(recorded) if recorded else None, bool(admission)


def _source_identity(path) -> tuple[int, int, int, int, int]:
    source = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(source.st_mode):
        raise SnapshotError("changed", "The staged source is no longer a regular file.")
    return (
        source.st_dev, source.st_ino, source.st_size,
        source.st_mtime_ns, source.st_ctime_ns,
    )


def acquisition_admission_preview(
    acquisition_id: int, client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Recheck current evidence without creating an admission or publishing bytes."""
    acquisition = ebook_acquisition_by_id(acquisition_id)
    if not acquisition or acquisition.get("status") != "verified":
        return _blocked("VERIFIED_ACQUISITION_REQUIRED", "No verified acquisition is current.")
    decision, current, recorded, existing_admission = _review_state(
        acquisition_id, int(acquisition["result_id"]),
    )
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
        source_identity = _source_identity(staged_path)
        if source_identity[2:4] != fingerprint[1:3]:
            return _blocked("STAGING_IDENTITY_CHANGED", "The staged file changed after inventory.")
        staged_hash = stable_file_fingerprint(
            staged_path, max_bytes=source_identity[2],
        ).split(":")[1]
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
        readiness = admission_readiness(client)
        if not readiness["ready"]:
            return _blocked(
                "ADMISSION_READINESS", "Admission readiness failed: "
                + ", ".join(readiness["blockers"]),
            )
        destination, bindery_path = _destination_for_result(result, staged_path)
        if bindery_path != str(result.get("stored_path") or ""):
            return _blocked("DESTINATION_MAPPING_CHANGED", "The Bindery destination mapping changed.")
        if _source_identity(staged_path) != source_identity or stable_file_fingerprint(
            staged_path, max_bytes=source_identity[2],
        ) != f"sha256:{staged_hash}:{source_identity[2]}":
            return _blocked("STAGED_BYTES_CHANGED", "Staged source identity changed during preview.")
        latest = ebook_acquisition_by_id(acquisition_id)
        final_decision, final_current, final_recorded, final_admission = _review_state(
            acquisition_id, int(acquisition["result_id"]),
        )
        if (
            latest != acquisition or final_admission
            or not final_recorded or final_recorded["id"] != plan["id"]
            or final_recorded["signature"] != plan["signature"]
            or final_recorded["evidence_revision"] != plan["evidenceRevision"]
            or not final_current or final_current["signature"] != plan["signature"]
            or final_current["evidenceRevision"] != plan["evidenceRevision"]
            or final_decision != decision or result_by_id(int(plan["resultId"])) != result
        ):
            return _blocked("DURABLE_STATE_CHANGED", "The admission review state changed during preview.")
    except (
        AcquisitionSafetyError, AdmissionSafetyError, BinderyClientError,
        StagingSafetyError, SnapshotError, OSError, ValueError, TypeError,
    ) as exc:
        return _blocked("DEPENDENCY_OR_IDENTITY_UNPROVEN", str(exc))
    return {
        "ok": True, "readOnly": True, "admissionAttempted": False,
        "acquisitionId": acquisition_id, "resultId": plan["resultId"],
        "bookId": plan["bookId"], "queueId": acquisition["queue_id"],
        "planSignature": plan["signature"],
        "evidenceRevision": plan["evidenceRevision"],
        "stagedRelativePath": fingerprint[0], "stagedSha256": staged_hash,
        "sourceIdentity": list(source_identity),
        "destination": str(destination), "binderyPath": bindery_path,
        "message": "Current evidence is suitable for review; publication remains disabled.",
    }
