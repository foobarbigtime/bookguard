"""Read-only proof for one admission that failed before publication began."""

from __future__ import annotations

import sqlite3
from typing import Any

from .acquisition import (
    AcquisitionSafetyError, _AWAITING_STAGING_QUEUE_STATUSES,
    _queue_payload, _queue_status,
)
from .acquisition_admission_preflight import _source_identity
from .acquisition_progress import _queue_identity, _verified_snapshot_matches
from .admission import (
    AdmissionSafetyError, _book_has_ebook, _destination_for_result,
    _exact_ebook_associations, _result_matches_book, admission_readiness,
)
from .bindery_client import BinderyClient, BinderyClientError
from .db import ebook_acquisition_by_id, ebook_admission_by_id, local_conn, result_by_id
from .file_snapshot import SnapshotError, stable_file_fingerprint
from .observe import _admission_decisions
from .recovery_planner import _build_plan, recovery_plan_by_id
from .staging import (
    StagingSafetyError, list_staged_ebooks, resolve_staged_file,
    verify_staged_ebook,
)


def _blocked(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False, "readOnly": True, "publicationAttempted": False,
        "reasonCode": code, "message": message,
    }


def prepublication_failure_preview(
    admission_id: int, client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Prove a current retry boundary without changing a journal or library byte."""
    admission = ebook_admission_by_id(admission_id)
    if not admission or (
        admission.get("status") != "failed"
        or admission.get("failure_stage") != "before_publication"
        or admission.get("publication_method")
    ):
        return _blocked("PREPUBLICATION_JOURNAL_REQUIRED", "The exact failed journal is unproven.")

    result_id = int(admission["result_id"])
    with local_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM ebook_admissions WHERE result_id=? OR stored_path=?",
            (result_id, admission["stored_path"]),
        ).fetchall()
        acquisitions = conn.execute(
            "SELECT id FROM ebook_acquisitions WHERE result_id=?",
            (result_id,),
        ).fetchall()
        decision = next((
            item for item in _admission_decisions(conn, 2000)
            if item.get("subjectKind") == "admission"
            and str(item.get("subjectId")) == str(admission_id)
        ), None)
        current = _build_plan(conn, decision) if decision else None
        recorded = conn.execute(
            """SELECT id, state, signature, evidence_revision FROM recovery_plans
               WHERE subject_kind='admission' AND subject_id=?
                 AND state IN ('planned', 'ready') ORDER BY id DESC LIMIT 1""",
            (str(admission_id),),
        ).fetchone()
    if (
        len(rows) != 1 or rows[0]["id"] != admission_id
        or len(acquisitions) != 1 or not current or not recorded
        or current.get("planKind") != "REVIEW_ADMISSION_PREPUBLICATION"
        or current.get("signature") != recorded["signature"]
        or current.get("evidenceRevision") != recorded["evidence_revision"]
    ):
        return _blocked("CURRENT_PLAN_UNPROVEN", "The failed admission has no unique current review.")
    plan = recovery_plan_by_id(int(recorded["id"]))
    acquisition = ebook_acquisition_by_id(int(acquisitions[0]["id"]))
    result = result_by_id(result_id)
    if not plan or not acquisition or not result or (
        plan.get("resultId") != result_id
        or plan.get("bookId") != admission["book_id"]
        or plan.get("path") != admission["stored_path"]
        or result.get("format") != "ebook"
        or acquisition.get("status") != "verified"
        or acquisition.get("admission_id") is not None
        or acquisition.get("result_id") != result_id
        or acquisition.get("book_id") != admission["book_id"]
        or admission.get("scan_id") != acquisition.get("scan_id")
        or admission.get("book_id") != result.get("book_id")
        or admission.get("scan_id") != result.get("scan_id")
        or admission.get("stored_path") != result.get("stored_path")
        or admission.get("local_path") != result.get("local_path")
        or admission.get("staged_relative_path") != acquisition.get("staged_relative_path")
        or admission.get("staged_sha256") not in (None, acquisition.get("staged_sha256"))
    ):
        return _blocked("DURABLE_IDENTITY_CHANGED", "The acquisition, result, or journal differs.")
    verified = admission.get("verification")
    if (
        verified is not None and (
            not isinstance(verified, dict)
            or verified.get("safeToAdmit") is not True
            or verified.get("sha256") != admission.get("staged_sha256")
        )
    ) or (admission.get("staged_sha256") and verified is None):
        return _blocked("JOURNAL_VERIFICATION_UNPROVEN", "The journal verification is incomplete.")

    client = client or BinderyClient()
    try:
        items, partial = _queue_payload(client)
        queue, exact, no_competing = _queue_identity(acquisition, items)
        if partial or not exact or not no_competing or (
            _queue_status(queue) not in _AWAITING_STAGING_QUEUE_STATUSES
        ):
            return _blocked("QUEUE_HANDOFF_UNPROVEN", "The exact queue handoff changed.")
        inventory = list_staged_ebooks(1000)
        staged = inventory.get("items") or []
        if inventory.get("truncated") or len(staged) != 1:
            return _blocked("STAGING_AMBIGUOUS", "Exactly one staged ebook is required.")
        fingerprint = (
            str(staged[0]["relativePath"]), int(staged[0]["size"]),
            int(staged[0]["modifiedNs"]),
        )
        if fingerprint[0] != acquisition["staged_relative_path"]:
            return _blocked("STAGING_IDENTITY_CHANGED", "The staged path changed.")
        _, staged_path = resolve_staged_file(fingerprint[0])
        source = _source_identity(staged_path)
        if source[2:4] != fingerprint[1:3]:
            return _blocked("STAGING_IDENTITY_CHANGED", "The staged source changed.")
        staged_hash = stable_file_fingerprint(
            staged_path, max_bytes=source[2],
        ).split(":")[1]
        if (
            staged_hash != acquisition.get("staged_sha256")
            or not _verified_snapshot_matches(
                acquisition, int(admission["book_id"]), fingerprint, staged_hash,
            )
        ):
            return _blocked("STAGED_BYTES_UNPROVEN", "The recorded staged bytes changed.")
        book = client.get_book(int(admission["book_id"]))
        if not _result_matches_book(result, book) or _book_has_ebook(book):
            return _blocked("BINDERY_BOOK_CHANGED", "The intended book or ebook state changed.")
        fresh = verify_staged_ebook(int(admission["book_id"]), fingerprint[0], client)
        if (
            fresh.get("safeToAdmit") is not True
            or fresh.get("sha256") != staged_hash
            or fresh.get("size") != fingerprint[1]
            or fresh.get("relativePath") != fingerprint[0]
        ):
            return _blocked("FRESH_VERIFICATION_FAILED", "Fresh staged verification failed.")
        destination, stored_path = _destination_for_result(result, staged_path)
        if stored_path != admission["stored_path"] or _exact_ebook_associations(stored_path):
            return _blocked("DESTINATION_OR_OWNER_CHANGED", "The destination or owner changed.")
        if not admission_readiness(client)["ready"]:
            return _blocked("ADMISSION_READINESS", "Admission readiness changed.")
        if _source_identity(staged_path) != source or stable_file_fingerprint(
            staged_path, max_bytes=source[2],
        ) != f"sha256:{staged_hash}:{source[2]}":
            return _blocked("STAGED_BYTES_CHANGED", "The staged source changed during review.")
        if ebook_admission_by_id(admission_id) != admission or (
            ebook_acquisition_by_id(int(acquisition["id"])) != acquisition
            or result_by_id(result_id) != result
            or recovery_plan_by_id(int(plan["id"])) != plan
        ):
            return _blocked("DURABLE_STATE_CHANGED", "The durable review state changed.")
        with local_conn() as conn:
            final_rows = conn.execute(
                "SELECT id FROM ebook_admissions WHERE result_id=? OR stored_path=?",
                (result_id, stored_path),
            ).fetchall()
            final_decision = next((
                item for item in _admission_decisions(conn, 2000)
                if item.get("subjectKind") == "admission"
                and str(item.get("subjectId")) == str(admission_id)
            ), None)
            final_plan = _build_plan(conn, final_decision) if final_decision else None
        if (
            len(final_rows) != 1 or final_rows[0]["id"] != admission_id
            or not final_plan or final_plan.get("signature") != plan["signature"]
            or final_plan.get("evidenceRevision") != plan["evidenceRevision"]
        ):
            return _blocked("DURABLE_STATE_CHANGED", "The review gained competing evidence.")
    except (
        AcquisitionSafetyError, AdmissionSafetyError, BinderyClientError,
        OSError, SnapshotError, StagingSafetyError, ValueError, TypeError,
        sqlite3.Error,
    ) as exc:
        return _blocked("DEPENDENCY_OR_IDENTITY_UNPROVEN", str(exc))
    return {
        "ok": True, "readOnly": True, "publicationAttempted": False,
        "admissionId": admission_id, "acquisitionId": int(acquisition["id"]),
        "resultId": result_id, "bookId": int(admission["book_id"]),
        "queueId": acquisition["queue_id"], "planSignature": plan["signature"],
        "evidenceRevision": plan["evidenceRevision"],
        "stagedRelativePath": fingerprint[0], "stagedSha256": staged_hash,
        "sourceIdentity": list(source), "destination": str(destination),
        "binderyPath": stored_path,
        "message": "Current evidence supports review; no retry or publication is enabled.",
    }
