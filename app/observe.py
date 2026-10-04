from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import load_automation_settings
from .verification_status import verification_is_inconclusive
from .db import local_conn, utc_now
from .recovery_classifier import classify_acquisition_failure, classify_admission_failure
from .recovery_planner import record_recovery_plans


POLICY_VERSION = "1"
_OBSERVE_LIMIT_MAX = 2000
_ACQUISITION_PROGRESS_STATES = {
    "preparing",
    "grab_requested",
    "queued",
    "downloading",
    "awaiting_staging",
    "staging_observed",
}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _decision(
    *,
    subject_kind: str,
    subject_id: int | str,
    state: str,
    decision: str,
    reason_code: str,
    reason: str,
    next_step: str,
    result_id: int | None = None,
    book_id: int | None = None,
    title: str = "",
    author: str = "",
    path: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "subjectKind": subject_kind,
        "subjectId": str(subject_id),
        "state": str(state or "").casefold(),
        "decision": str(decision or "").casefold(),
        "reasonCode": reason_code,
        "reason": reason,
        "nextStep": next_step,
        "resultId": result_id,
        "bookId": book_id,
        "title": title,
        "author": author,
        "path": path,
        "evidence": evidence or {},
    }


def _latest_verifications(conn, result_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not result_ids or not _table_exists(conn, "content_verifications"):
        return {}
    placeholders = ",".join("?" for _ in result_ids)
    rows = conn.execute(
        f"""
        SELECT id, result_id, verdict, confidence, source, updated_at, evidence_json
        FROM content_verifications
        WHERE result_id IN ({placeholders})
        ORDER BY result_id, updated_at DESC, id DESC
        """,
        result_ids,
    ).fetchall()
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:
        result_id = int(row["result_id"])
        if result_id not in latest:
            latest[result_id] = dict(row)
    current: dict[int, dict[str, Any]] = {}
    for result_id, item in latest.items():
        try:
            evidence = json.loads(item.pop("evidence_json") or "{}")
        except (TypeError, ValueError):
            evidence = {}
        # A scanner outage proves nothing about the file: plan re-verification,
        # never quarantine.
        if not verification_is_inconclusive(item.get("verdict"), evidence):
            current[result_id] = item
    return current


def _result_decisions(conn, limit: int) -> list[dict[str, Any]]:
    scan = conn.execute(
        "SELECT id, status FROM scans ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if not scan or str(scan["status"] or "").casefold() != "complete":
        return []

    rows = conn.execute(
        """
        SELECT id, scan_id, file_id, book_id, author, title, format, stored_path,
               classification, reason_code, risk_score
        FROM scan_results
        WHERE scan_id=?
          AND (
              classification IN ('REVIEW', 'REJECT')
              OR id IN (
                  SELECT result_id
                  FROM content_verifications
                  WHERE verdict != 'VERIFIED_CORRECT'
              )
          )
        ORDER BY
            CASE classification
                WHEN 'REJECT' THEN 0
                WHEN 'REVIEW' THEN 1
                ELSE 2
            END,
            risk_score DESC,
            id DESC
        LIMIT ?
        """,
        (scan["id"], limit),
    ).fetchall()
    result_ids = [int(row["id"]) for row in rows]
    verifications = _latest_verifications(conn, result_ids)

    decisions: list[dict[str, Any]] = []
    for row in rows:
        result_id = int(row["id"])
        classification = str(row["classification"] or "").upper()
        fmt = str(row["format"] or "").casefold()
        verification = verifications.get(result_id)
        verdict = str((verification or {}).get("verdict") or "").upper()

        common = {
            "subject_kind": "result",
            "subject_id": result_id,
            "result_id": result_id,
            "book_id": int(row["book_id"]) if row["book_id"] is not None else None,
            "title": str(row["title"] or ""),
            "author": str(row["author"] or ""),
            "path": str(row["stored_path"] or ""),
        }
        evidence = {
            "scanId": str(row["scan_id"] or ""),
            "scanClassification": classification,
            "scanReasonCode": str(row["reason_code"] or "UNKNOWN"),
            "riskScore": int(row["risk_score"] or 0),
            "format": fmt,
        }
        if verification:
            evidence["verification"] = {
                "id": verification["id"],
                "verdict": verdict,
                "confidence": int(verification["confidence"] or 0),
                "source": str(verification["source"] or ""),
                "updatedAt": verification["updated_at"],
            }

        if verdict == "VERIFIED_CORRECT":
            decisions.append(_decision(
                **common,
                state=verdict,
                decision="no_action",
                reason_code="VERIFIED_CORRECT_NO_ACTION",
                reason=(
                    "Durable content verification confirms the expected media "
                    "identity, so Observe Mode proposes no remediation."
                ),
                next_step="No automatic remediation is proposed for this result.",
                evidence=evidence,
            ))
        elif verdict == "WRONG_CONTENT":
            decisions.append(_decision(
                **common,
                state=verdict,
                decision="would_resolve_wrong_content",
                reason_code="VERIFIED_WRONG_CONTENT",
                reason=(
                    "Content verification identifies the media as different from "
                    "the expected Bindery book."
                ),
                next_step=(
                    "A future Automatic Mode should quarantine/detach the proven "
                    "wrong association, resolve any proven owner, and reacquire the "
                    "missing expected media. Observe Mode records the plan only."
                ),
                evidence=evidence,
            ))
        elif verdict == "WRONG_MEDIA_TYPE":
            decisions.append(_decision(
                **common,
                state=verdict,
                decision="would_resolve_media_mismatch",
                reason_code="WRONG_MEDIA_TYPE",
                reason=(
                    "Deterministic media inspection proves that the tracked bytes are "
                    "a different media kind than Bindery expects."
                ),
                next_step=(
                    "A future Automatic Mode should correct the proven association, "
                    "then reacquire and verify whichever expected media remains missing. "
                    "Observe Mode performs no mutation."
                ),
                evidence=evidence,
            ))
        elif verdict == "UNSAFE_FILE":
            decisions.append(_decision(
                **common,
                state=verdict,
                decision="would_quarantine_unsafe",
                reason_code="UNSAFE_FILE",
                reason="A deterministic file-safety or integrity check failed.",
                next_step=(
                    "A future Automatic Mode should remove the exact proven unsafe "
                    "media from active use through the quarantine-first workflow and "
                    "reacquire a verified replacement when required."
                ),
                evidence=evidence,
            ))
        elif verdict == "INSUFFICIENT_EVIDENCE":
            decisions.append(_decision(
                **common,
                state=verdict,
                decision="attention",
                reason_code="INSUFFICIENT_EVIDENCE",
                reason=(
                    "BookGuard does not have enough deterministic identity evidence "
                    "to choose an automatic action."
                ),
                next_step=(
                    "Leave this item for operator review unless stronger verification "
                    "evidence becomes available."
                ),
                evidence=evidence,
            ))
        elif verdict == "METADATA_ERROR":
            decisions.append(_decision(
                **common,
                state=verdict,
                decision="would_repair_metadata",
                reason_code="METADATA_REPAIR_REVIEW",
                reason=(
                    "Content identity appears usable but the durable verification "
                    "record reports a metadata problem."
                ),
                next_step=(
                    "A future Automatic Mode should apply the existing guarded "
                    "metadata repair after re-verifying the same evidence at the "
                    "write boundary. Observe Mode records the plan only."
                ),
                evidence=evidence,
            ))
        elif not verification and classification in {"REVIEW", "REJECT"}:
            if fmt == "ebook":
                decisions.append(_decision(
                    **common,
                    state=classification,
                    decision="would_verify_result",
                    reason_code="CONTENT_VERIFICATION_REQUIRED",
                    reason=(
                        "The latest scan requires review, but no durable ebook content "
                        "verification is available for this result."
                    ),
                    next_step=(
                        "A future Automatic Mode should run the existing read-only "
                        "content verifier before considering remediation or admission. "
                        "Observe Mode records that proposal without reading library bytes."
                    ),
                    evidence=evidence,
                ))
            else:
                decisions.append(_decision(
                    **common,
                    state=classification,
                    decision="would_verify_audiobook",
                    reason_code="AUDIOBOOK_VERIFICATION_REQUIRED",
                    reason=(
                        "The latest scan requires review, but no durable audiobook "
                        "evidence result is available for this item."
                    ),
                    next_step=(
                        "A future Automatic Mode should run the read-only audiobook "
                        "media-kind, technical-integrity, and identity evidence engine. "
                        "Observe Mode records that proposal without reading library bytes."
                    ),
                    evidence=evidence,
                ))
    return decisions


def _durable_admission_review_snapshot(row) -> tuple[bool, dict[str, Any]]:
    """Review persisted identity only; a future executor must recheck live bytes."""
    try:
        verification = json.loads(row["verification_json"] or "{}")
    except (TypeError, ValueError):
        verification = {}
    if not isinstance(verification, dict):
        verification = {}
    staged_path = str(row["staged_relative_path"] or "")
    staged_sha = str(row["staged_sha256"] or "")
    revision = hashlib.sha256(
        json.dumps(verification, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    proven = all((
        row["admission_id"] is None,
        isinstance(row["result_id"], int) and row["result_id"] > 0,
        isinstance(row["book_id"], int) and row["book_id"] > 0,
        row["linked_result_id"] == row["result_id"],
        row["linked_book_id"] == row["book_id"],
        isinstance(row["queue_id"], int) and row["queue_id"] > 0,
        str(row["queue_status"] or "").casefold() in {
            "completed", "importexternal", "importheld", "importpending", "importing",
        },
        bool(staged_path) and not staged_path.startswith("/")
        and all(part not in {"", ".", ".."} for part in staged_path.split("/")),
        len(staged_sha) == 64 and all(char in "0123456789abcdef" for char in staged_sha),
        verification.get("safeToAdmit") is True,
        verification.get("stableDuringVerification") is True,
        verification.get("verdict") == "VERIFIED_CORRECT",
        verification.get("bookId") == row["book_id"],
        verification.get("relativePath") == staged_path,
        isinstance(verification.get("size"), int)
        and not isinstance(verification.get("size"), bool)
        and verification["size"] > 0,
        verification.get("sha256") == staged_sha,
        isinstance(verification.get("confidence"), int)
        and not isinstance(verification.get("confidence"), bool)
        and verification["confidence"] >= 99,
        verification.get("admissionBlockers") == [],
    ))
    return proven, {
        "stagedRelativePath": staged_path,
        "stagedSha256": staged_sha,
        "verificationRevision": revision,
        "verifiedSnapshotMatches": proven,
    }


def _acquisition_decisions(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "ebook_acquisitions"):
        return []
    rows = conn.execute(
        """
        SELECT a.id, a.result_id, a.book_id, a.status, a.admission_id,
               a.replacement_for_quarantine_plan_id,
               a.candidate_guid, a.candidate_title, a.candidate_indexer,
               a.candidate_protocol, a.queue_id, a.queue_status,
               a.observed_relative_path, a.staged_relative_path, a.staged_sha256,
               a.verification_json, a.error, r.id AS linked_result_id,
               r.book_id AS linked_book_id, r.title, r.author, r.stored_path
        FROM ebook_acquisitions a
        LEFT JOIN scan_results r ON r.id=a.result_id
        ORDER BY a.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    decisions = []
    for row in rows:
        status = str(row["status"] or "").casefold()
        common = {
            "subject_kind": "acquisition",
            "subject_id": row["id"],
            "result_id": int(row["result_id"]) if row["result_id"] is not None else None,
            "book_id": int(row["book_id"]) if row["book_id"] is not None else None,
            "title": str(row["title"] or ""),
            "author": str(row["author"] or ""),
            "path": str(
                row["staged_relative_path"]
                or row["observed_relative_path"]
                or row["stored_path"]
                or ""
            ),
        }
        evidence = {
            "acquisitionId": row["id"],
            "status": status,
            "admissionId": row["admission_id"],
            "candidateGuid": str(row["candidate_guid"] or ""),
            "candidateTitle": str(row["candidate_title"] or ""),
            "candidateIndexer": str(row["candidate_indexer"] or ""),
            "candidateProtocol": str(row["candidate_protocol"] or ""),
            "queueId": row["queue_id"],
            "queueStatus": str(row["queue_status"] or ""),
            "recordedError": str(row["error"] or ""),
        }
        if row["replacement_for_quarantine_plan_id"] is not None:
            evidence["replacementForQuarantinePlanId"] = int(
                row["replacement_for_quarantine_plan_id"]
            )
        linked_admission = None
        linked_admission_status = ""
        linked_admission_identity_matches = False
        if status == "admitted" and row["admission_id"] is not None:
            linked_admission = conn.execute(
                """
                SELECT id, result_id, book_id, status, staged_relative_path,
                       staged_sha256
                FROM ebook_admissions
                WHERE id=?
                LIMIT 1
                """,
                (int(row["admission_id"]),),
            ).fetchone()
            if linked_admission is not None:
                linked_admission_status = str(
                    linked_admission["status"] or ""
                ).casefold()
                linked_admission_identity_matches = all((
                    int(linked_admission["result_id"] or 0)
                    == int(row["result_id"] or 0),
                    int(linked_admission["book_id"] or 0)
                    == int(row["book_id"] or 0),
                    str(linked_admission["staged_relative_path"] or "")
                    == str(row["staged_relative_path"] or ""),
                    bool(str(row["staged_sha256"] or "")),
                    str(linked_admission["staged_sha256"] or "")
                    == str(row["staged_sha256"] or ""),
                ))
            evidence["admissionStatus"] = linked_admission_status
            evidence["linkedAdmissionIdentityMatches"] = (
                linked_admission_identity_matches
            )

        if status in _ACQUISITION_PROGRESS_STATES:
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_reconcile_acquisition",
                reason_code="ACQUISITION_PROGRESSABLE",
                reason=(
                    "The durable acquisition is in a coordinator-managed state that "
                    "can be re-observed without choosing a new release."
                ),
                next_step=(
                    "Observe Mode records this proposed reconciliation only; it does "
                    "not contact Bindery or change queue/staging state."
                ),
                evidence=evidence,
            ))
        elif status == "verified":
            retired = conn.execute(
                """SELECT id FROM ebook_admissions
                   WHERE result_id=? AND status='retired_before_publication'
                   ORDER BY id""",
                (int(row["result_id"]),),
            ).fetchall()
            if retired:
                evidence["retiredAdmissionIds"] = [int(item["id"]) for item in retired]
            proven, snapshot = _durable_admission_review_snapshot(row)
            evidence.update(snapshot)
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_review_verified_acquisition" if proven else "attention",
                reason_code=(
                    "VERIFIED_ACQUISITION_REVIEW_AVAILABLE" if proven
                    else "ACQUISITION_VERIFICATION_UNPROVEN"
                ),
                reason=(
                    "The durable verified acquisition has an exact staged identity "
                    "for a separate admission review."
                    if proven else
                    "The verified acquisition's durable identity or safety snapshot "
                    "is incomplete or contradictory."
                ),
                next_step=(
                    "Review the proposed admission; no publication is enabled. Fresh "
                    "staged bytes, book identity, queue handoff, destination, and "
                    "readiness must be proven before any future mutation."
                    if proven else
                    "Inspect the acquisition and repair the evidence through an explicit "
                    "guarded workflow before considering admission."
                ),
                evidence=evidence,
            ))
        elif (
            status == "admitted"
            and linked_admission_status == "registered"
            and linked_admission_identity_matches
        ):
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_finalize_acquisition",
                reason_code="FINALIZATION_RECOVERY_AVAILABLE",
                reason=(
                    "The acquisition's linked admission is durably registered and "
                    "matches the acquisition's result, book, staged path, and hash."
                ),
                next_step=(
                    "Observe Mode records the guarded finalization plan only. It does "
                    "not remove the terminal queue record or staged copy."
                ),
                evidence=evidence,
            ))
        elif status == "admitted" and linked_admission_status == "registered":
            decisions.append(_decision(
                **common,
                state=status,
                decision="attention",
                reason_code="ACQUISITION_ADMISSION_IDENTITY_MISMATCH",
                reason=(
                    "The linked admission is registered, but its durable result, book, "
                    "staged path, or staged hash does not match the acquisition."
                ),
                next_step=(
                    "Review the acquisition/admission link before any finalization or "
                    "cleanup is attempted."
                ),
                evidence=evidence,
            ))
        elif status == "admitted":
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_reconcile_admission",
                reason_code="ADMISSION_REGISTRATION_PENDING",
                reason=(
                    "The acquisition has a durable admission and would next verify "
                    "Bindery registration."
                ),
                next_step=(
                    "Observe Mode records the proposed registration reconciliation "
                    "without contacting Bindery."
                ),
                evidence=evidence,
            ))
        elif status in {"finalizing", "cleanup_required"}:
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_finalize_acquisition",
                reason_code="FINALIZATION_RECOVERY_AVAILABLE",
                reason=(
                    "The durable acquisition is in a finalization/recovery state that "
                    "the guarded workflow already knows how to resume."
                ),
                next_step=(
                    "Observe Mode records the proposed finalization only. No queue or "
                    "staging cleanup is performed."
                ),
                evidence=evidence,
            ))
        elif status == "finalized":
            decisions.append(_decision(
                **common,
                state=status,
                decision="no_action",
                reason_code="ACQUISITION_FINALIZED",
                reason="The acquisition is already finalized.",
                next_step="No automatic work is proposed for this acquisition.",
                evidence=evidence,
            ))
        else:
            recovery = classify_acquisition_failure(status, str(row["error"] or ""))
            if recovery and recovery.recoverable:
                evidence["recoveryClassification"] = {
                    "planKind": recovery.plan_kind,
                    "retrySameOperation": recovery.retry_same_operation,
                    "maxRetries": recovery.max_retries,
                    "backoffSeconds": list(recovery.backoff_seconds),
                }
                decisions.append(_decision(
                    **common,
                    state=status,
                    decision="would_recover_acquisition_failure",
                    reason_code=recovery.reason_code,
                    reason=recovery.explanation,
                    next_step=(
                        "A future Automatic Mode should execute the classified recovery "
                        "plan only after all current readiness, identity, queue, and "
                        "candidate gates are freshly revalidated. Observe Mode records "
                        "the plan only."
                    ),
                    evidence=evidence,
                ))
            else:
                decisions.append(_decision(
                    **common,
                    state=status,
                    decision="attention",
                    reason_code=(
                        "ACQUISITION_REVIEW_REQUIRED"
                        if status in {"review_required", "failed"}
                        else "UNKNOWN_ACQUISITION_STATE"
                    ),
                    reason=(
                        str(row["error"] or "")
                        or "The acquisition state is not allowlisted for automatic progress."
                    ),
                    next_step="Review the durable acquisition before any further action.",
                    evidence=evidence,
                ))
    return decisions


def _admission_decisions(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "ebook_admissions"):
        return []
    rows = conn.execute(
        """
        SELECT a.id, a.result_id, a.book_id, a.status, a.staged_relative_path,
               a.staged_sha256, a.publication_method, a.failure_stage,
               a.verification_json, a.stored_path, a.local_path,
               a.error, r.title, r.author
        FROM ebook_admissions a
        LEFT JOIN scan_results r ON r.id=a.result_id
        ORDER BY a.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    decisions = []
    for row in rows:
        status = str(row["status"] or "").casefold()
        common = {
            "subject_kind": "admission",
            "subject_id": row["id"],
            "result_id": int(row["result_id"]) if row["result_id"] is not None else None,
            "book_id": int(row["book_id"]) if row["book_id"] is not None else None,
            "title": str(row["title"] or ""),
            "author": str(row["author"] or ""),
            "path": str(row["stored_path"] or row["local_path"] or row["staged_relative_path"] or ""),
        }
        evidence = {
            "admissionId": row["id"],
            "status": status,
            "stagedRelativePath": str(row["staged_relative_path"] or ""),
            "stagedSha256": str(row["staged_sha256"] or ""),
            "publicationMethod": str(row["publication_method"] or ""),
            "failureStage": str(row["failure_stage"] or ""),
            "recordedError": str(row["error"] or ""),
        }
        if status == "retired_before_publication":
            decisions.append(_decision(
                **common,
                state=status,
                decision="no_action",
                reason_code="ADMISSION_RETIRED_BEFORE_PUBLICATION",
                reason="The failed journal was retained after proof that publication had not begun.",
                next_step="Review the separately guarded acquisition admission plan.",
                evidence=evidence,
            ))
        elif status == "registered":
            decisions.append(_decision(
                **common,
                state=status,
                decision="no_action",
                reason_code="ADMISSION_REGISTERED",
                reason="The admitted ebook is already registered to the intended book.",
                next_step="No automatic work is proposed for this admission.",
                evidence=evidence,
            ))
        elif status == "scan_requested":
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_reconcile_admission",
                reason_code="REGISTRATION_SCAN_PENDING",
                reason=(
                    "The admitted ebook is waiting for Bindery registration to be "
                    "observed and reconciled."
                ),
                next_step=(
                    "Observe Mode records the proposed check only and does not call "
                    "Bindery."
                ),
                evidence=evidence,
            ))
        elif status == "scan_request_failed" and (
            row["staged_sha256"] and row["publication_method"] and row["error"]
        ):
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_reconcile_admission",
                reason_code="REGISTRATION_SCAN_OUTCOME_UNKNOWN",
                reason=(
                    "The verified ebook was published, but Bindery's scan request "
                    "failed or its outcome is uncertain."
                ),
                next_step=(
                    "Adopt registration only if the exact published and staged bytes "
                    "still match and Bindery independently proves the intended owner. "
                    "Never send a second scan for this uncertain request."
                ),
                evidence=evidence,
            ))
        elif status == "registration_conflict":
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_correct_registration_conflict",
                reason_code="REGISTRATION_CONFLICT",
                reason=(
                    "Bindery assigns the admitted ebook's exact published path to a "
                    "different book, while BookGuard retains the intended admission "
                    "identity and staged snapshot."
                ),
                next_step=(
                    "Observe Mode records the guarded exact-path ownership correction "
                    "plan only. It does not remove a queue record, change Bindery import "
                    "mode, reassign ownership, or alter library/staged bytes."
                ),
                evidence=evidence,
            ))
        elif status == "published":
            linked = conn.execute(
                """SELECT id, status, result_id, book_id, staged_relative_path,
                          staged_sha256 FROM ebook_acquisitions WHERE admission_id=?""",
                (int(row["id"]),),
            ).fetchall()
            try:
                verification = json.loads(row["verification_json"] or "{}")
            except (TypeError, ValueError):
                verification = {}
            if not isinstance(verification, dict):
                verification = {}
            proven = (
                len(linked) == 1
                and linked[0]["status"] == "admitted"
                and linked[0]["result_id"] == row["result_id"]
                and linked[0]["book_id"] == row["book_id"]
                and linked[0]["staged_relative_path"] == row["staged_relative_path"]
                and linked[0]["staged_sha256"] == row["staged_sha256"]
                and verification.get("safeToAdmit") is True
                and verification.get("sha256") == row["staged_sha256"]
                and bool(row["publication_method"] and row["staged_sha256"])
            )
            evidence["acquisitionId"] = linked[0]["id"] if proven else None
            decisions.append(_decision(
                **common,
                state=status,
                decision="would_request_published_admission_scan" if proven else "attention",
                reason_code=(
                    "PUBLISHED_ACQUISITION_ADMISSION_SCAN_READY" if proven
                    else "PUBLISHED_ADMISSION_PROVENANCE_UNCLEAR"
                ),
                reason=(
                    "A linked verified acquisition has a published admission awaiting "
                    "a separately authorized Bindery scan."
                    if proven else "Published admission provenance is incomplete."
                ),
                next_step=(
                    "Recheck the exact publication receipt, bytes, identity, ownership, "
                    "and readiness before one allowlisted scan request."
                    if proven else "Review the durable admission and acquisition link."
                ),
                evidence=evidence,
            ))
        else:
            recovery = classify_admission_failure(
                status,
                str(row["error"] or ""),
                str(row["publication_method"] or ""),
                failure_stage=str(row["failure_stage"] or ""),
                verified_snapshot=bool(
                    row["staged_sha256"] and row["verification_json"]
                ),
            )
            if recovery and recovery.recoverable:
                evidence["recoveryClassification"] = {
                    "planKind": recovery.plan_kind,
                    "retrySameOperation": recovery.retry_same_operation,
                    "maxRetries": recovery.max_retries,
                    "backoffSeconds": list(recovery.backoff_seconds),
                }
                decisions.append(_decision(
                    **common,
                    state=status,
                    decision="would_recover_admission_failure",
                    reason_code=recovery.reason_code,
                    reason=recovery.explanation,
                    next_step=(
                        "A future Automatic Mode should execute the classified admission "
                        "recovery only after staged bytes, destination, book identity, "
                        "filesystem topology, and readiness are freshly revalidated. "
                        "Observe Mode records the plan only."
                    ),
                    evidence=evidence,
                ))
                continue
            mapping = {
                "registration_conflict": (
                    "REGISTRATION_CONFLICT",
                    "Bindery registration conflicts with the intended book.",
                    "Review the conflict and use the explicit guarded correction path only after confirming the intended mapping.",
                ),
                "registration_correcting": (
                    "REGISTRATION_CORRECTION_INTERRUPTED",
                    "A guarded registration correction was interrupted.",
                    "Review and explicitly recover the durable correction state.",
                ),
                "failed": (
                    "ADMISSION_FAILED",
                    str(row["error"] or "") or "The guarded admission failed.",
                    "Review the durable admission and verification evidence before retrying.",
                ),
                "preparing": (
                    "ADMISSION_INCOMPLETE",
                    "An admission record exists but has not reached the registration scan stage.",
                    "Review the durable admission state before attempting recovery.",
                ),
                "scan_requesting": (
                    "REGISTRATION_SCAN_OUTCOME_UNCERTAIN",
                    "A one-shot scan claim exists, but its external outcome is unproven.",
                    "Review Bindery ownership; never send another automatic scan.",
                ),
            }
            reason_code, reason, next_step = mapping.get(
                status,
                (
                    "UNKNOWN_ADMISSION_STATE",
                    "The admission state is not allowlisted for automatic progress.",
                    "Review the durable admission before any further action.",
                ),
            )
            decisions.append(_decision(
                **common,
                state=status,
                decision="attention",
                reason_code=reason_code,
                reason=reason,
                next_step=next_step,
                evidence=evidence,
            ))
    return decisions


def _signature(item: dict[str, Any]) -> str:
    payload = {
        "policyVersion": POLICY_VERSION,
        "subjectKind": item["subjectKind"],
        "subjectId": item["subjectId"],
        "state": item["state"],
        "decision": item["decision"],
        "reasonCode": item["reasonCode"],
        "resultId": item.get("resultId"),
        "bookId": item.get("bookId"),
        "evidence": item.get("evidence") or {},
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _persist_decision(conn, item: dict[str, Any], now: str) -> dict[str, Any]:
    signature = _signature(item)
    conn.execute(
        """
        INSERT INTO automation_observations(
            signature, policy_version, mode, subject_kind, subject_id,
            result_id, book_id, title, author, path, state, decision,
            reason_code, reason, evidence_json, first_seen_at, last_seen_at,
            observed_count
        ) VALUES (?, ?, 'observe', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(signature) DO UPDATE SET
            title=excluded.title,
            author=excluded.author,
            path=excluded.path,
            reason=excluded.reason,
            evidence_json=excluded.evidence_json,
            last_seen_at=excluded.last_seen_at,
            observed_count=automation_observations.observed_count + 1
        """,
        (
            signature,
            POLICY_VERSION,
            item["subjectKind"],
            item["subjectId"],
            item.get("resultId"),
            item.get("bookId"),
            item.get("title") or "",
            item.get("author") or "",
            item.get("path") or "",
            item["state"],
            item["decision"],
            item["reasonCode"],
            item["reason"],
            json.dumps(
                {
                    **(item.get("evidence") or {}),
                    "nextStep": item.get("nextStep") or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM automation_observations WHERE signature=? LIMIT 1",
        (signature,),
    ).fetchone()
    return _decode_row(row)


def _decode_row(row) -> dict[str, Any]:
    item = dict(row)
    raw = item.pop("evidence_json", "{}")
    try:
        evidence = json.loads(raw)
    except json.JSONDecodeError:
        evidence = {"decodeError": True}
    return {
        "id": item["id"],
        "signature": item["signature"],
        "policyVersion": item["policy_version"],
        "mode": item["mode"],
        "subjectKind": item["subject_kind"],
        "subjectId": item["subject_id"],
        "resultId": item["result_id"],
        "bookId": item["book_id"],
        "title": item["title"],
        "author": item["author"],
        "path": item["path"],
        "state": item["state"],
        "decision": item["decision"],
        "reasonCode": item["reason_code"],
        "reason": item["reason"],
        "evidence": evidence,
        "nextStep": str(evidence.get("nextStep") or ""),
        "firstSeenAt": item["first_seen_at"],
        "lastSeenAt": item["last_seen_at"],
        "observedCount": item["observed_count"],
    }


def run_observe_cycle(limit: int = 500) -> dict[str, Any]:
    """Record proposed automation decisions without invoking any mutation workflow."""
    configured = load_automation_settings()
    if configured.automation_mode != "observe":
        return {
            "mode": configured.automation_mode,
            "enabled": False,
            "state": "disabled",
            "policyVersion": POLICY_VERSION,
            "decisionCount": 0,
            "planCount": 0,
            "records": [],
            "plans": [],
            "message": (
                "Observe Mode is disabled. Set BOOKGUARD_AUTOMATION_MODE=observe "
                "to record non-mutating automation decisions."
            ),
        }

    limit = max(1, min(int(limit), _OBSERVE_LIMIT_MAX))
    now = utc_now()
    with local_conn() as conn:
        if not _table_exists(conn, "automation_observations"):
            raise RuntimeError(
                "Observe Mode journal is unavailable. Restart BookGuard so database initialization can complete."
            )
        decisions = (
            _acquisition_decisions(conn, limit)
            + _admission_decisions(conn, limit)
            + _result_decisions(conn, limit)
        )
        records = [_persist_decision(conn, item, now) for item in decisions]
        plans = record_recovery_plans(conn, decisions, now=now)
        conn.commit()

    return {
        "mode": "observe",
        "enabled": True,
        "state": "observed",
        "policyVersion": POLICY_VERSION,
        "decisionCount": len(records),
        "planCount": len(plans),
        "records": records,
        "plans": plans,
        "message": (
            "Observe Mode recorded proposed decisions and non-executable recovery "
            "plans only. No Bindery, queue, staging, quarantine, metadata, or "
            "library mutation was attempted."
        ),
    }


def observe_snapshot(limit: int = 100) -> dict[str, Any]:
    configured = load_automation_settings()
    limit = max(1, min(int(limit), 1000))
    with local_conn() as conn:
        if not _table_exists(conn, "automation_observations"):
            rows = []
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM automation_observations
                ORDER BY last_seen_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    items = [_decode_row(row) for row in rows]
    return {
        "mode": configured.automation_mode,
        "enabled": configured.automation_mode == "observe",
        "policyVersion": POLICY_VERSION,
        "count": len(items),
        "items": items,
    }


def observe_attention_items(limit: int = 200) -> list[dict[str, Any]]:
    configured = load_automation_settings()
    if configured.automation_mode != "observe":
        return []
    limit = max(1, min(int(limit), 500))
    with local_conn() as conn:
        if not _table_exists(conn, "automation_observations"):
            return []
        rows = conn.execute(
            """
            SELECT *
            FROM automation_observations
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (limit * 4,),
        ).fetchall()

    latest_scan = None
    with local_conn() as conn:
        scan_row = conn.execute(
            "SELECT id FROM scans ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        latest_scan = str(scan_row["id"]) if scan_row else None

    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for row in rows:
        decoded = _decode_row(row)
        subject = (decoded["subjectKind"], decoded["subjectId"])
        if subject in seen:
            continue
        seen.add(subject)
        if decoded["decision"] != "attention":
            continue
        if (
            decoded["subjectKind"] == "result"
            and latest_scan is not None
            and str(decoded["evidence"].get("scanId") or "") != latest_scan
        ):
            continue
        workflow = "/review"
        items.append({
            "kind": "observe",
            "kindLabel": "Observe Mode",
            "id": decoded["id"],
            "status": "attention",
            "title": decoded["title"],
            "author": decoded["author"],
            "message": decoded["reason"],
            "guidance": {
                "label": decoded["reasonCode"].replace("_", " ").title(),
                "why": decoded["reason"],
                "nextStep": decoded["nextStep"],
                "recordedError": "",
            },
            "updatedAt": decoded["lastSeenAt"],
            "detailHref": f"/activity/observe/{decoded['id']}",
            "href": workflow,
        })
        if len(items) >= limit:
            break
    return items
