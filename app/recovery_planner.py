from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from .db import local_conn, utc_now


PLANNER_VERSION = "2"
_PLAN_STATES_ACTIVE = {"planned", "ready", "retry_wait", "blocked"}
_PLAN_DECISIONS = {
    "would_resolve_wrong_content",
    "would_resolve_media_mismatch",
    "would_quarantine_unsafe",
    "would_repair_metadata",
    "would_reconcile_acquisition",
    "would_review_verified_acquisition",
    "would_reconcile_admission",
    "would_request_published_admission_scan",
    "would_correct_registration_conflict",
    "would_finalize_acquisition",
    "would_recover_acquisition_failure",
    "would_recover_admission_failure",
}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _decision_evidence_for_revision(
    plan_kind: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Return safety-significant decision evidence for durable plan identity.

    A bounded same-operation transient retry is authorized by the stable
    recovery classification and durable subject/candidate/workflow identity.
    The raw transport error is diagnostic and may legitimately change between
    attempts without changing that authorization.
    """
    normalized = dict(evidence)
    if plan_kind == "RETRY_ACQUISITION_TRANSIENT":
        normalized.pop("recordedError", None)
    return normalized


def _json_object(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _latest_verification_context(conn, result_id: int | None) -> dict[str, Any]:
    if not result_id or not _table_exists(conn, "content_verifications"):
        return {}
    row = conn.execute(
        """
        SELECT id, signature, result_id, scan_id, file_id, book_id, format,
               target_path, file_fingerprint, verdict, confidence, source,
               evidence_json, updated_at
        FROM content_verifications
        WHERE result_id=?
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (int(result_id),),
    ).fetchone()
    if row is None:
        return {}
    item = dict(row)
    evidence = _json_object(item.pop("evidence_json", "{}"))
    revision = _stable_hash(
        {
            "signature": item.get("signature"),
            "verdict": item.get("verdict"),
            "confidence": item.get("confidence"),
            "source": item.get("source"),
            "fileFingerprint": item.get("file_fingerprint"),
            "targetPath": item.get("target_path"),
            "evidence": evidence,
        }
    )
    return {
        **item,
        "evidence": evidence,
        "reasonCode": str(evidence.get("reasonCode") or ""),
        "revision": revision,
    }


def _result_context(conn, result_id: int | None) -> dict[str, Any]:
    if not result_id:
        return {}
    row = conn.execute(
        """
        SELECT id, scan_id, file_id, book_id, format, stored_path, local_path,
               classification, reason_code
        FROM scan_results
        WHERE id=?
        LIMIT 1
        """,
        (int(result_id),),
    ).fetchone()
    return dict(row) if row else {}


def _step(
    code: str,
    description: str,
    *,
    external_mutation: bool = False,
    stop_if_unproven: bool = False,
) -> dict[str, Any]:
    return {
        "code": code,
        "description": description,
        "externalMutation": external_mutation,
        "stopIfUnproven": stop_if_unproven,
    }


def _wrong_content_plan(reason_code: str) -> tuple[str, list[dict[str, Any]]]:
    if reason_code == "MIXED_AUDIO_CONTENT":
        return (
            "RECOVER_MIXED_AUDIO_CONTENT",
            [
                _step(
                    "revalidate_whole_set",
                    "Re-inventory the complete tracked audiobook set and reproduce the deterministic mixed-content verdict.",
                    stop_if_unproven=True,
                ),
                _step(
                    "derive_file_disposition",
                    "Classify each readable member as expected, proven foreign, or ambiguous; ambiguity blocks mutation for that member.",
                    stop_if_unproven=True,
                ),
                _step(
                    "resolve_proven_associations",
                    "Resolve any exact file-to-book ownership that can be proven from current Bindery and byte/path evidence.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
                _step(
                    "quarantine_proven_foreign_media",
                    "Quarantine only media proven foreign to the expected book, preserving provenance and exact byte identity.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
                _step(
                    "reacquire_missing_expected_media",
                    "Reacquire expected audiobook media only if the remaining verified set is incomplete.",
                    external_mutation=True,
                ),
                _step(
                    "verify_replacement_set",
                    "Run the full E2 media-kind, technical-integrity, and identity verifier over the resulting audiobook set.",
                    stop_if_unproven=True,
                ),
                _step(
                    "reconcile_final_state",
                    "Confirm the final Bindery registration and library bytes match the expected book with no unresolved foreign members.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )
    if reason_code == "MUSIC_MISMATCH":
        return (
            "RECOVER_NONBOOK_AUDIO",
            [
                _step(
                    "revalidate_wrong_content",
                    "Re-run deterministic media identity checks and reproduce the non-book/music mismatch.",
                    stop_if_unproven=True,
                ),
                _step(
                    "capture_exact_source_identity",
                    "Capture the exact current path and byte identity before any future mutation.",
                    stop_if_unproven=True,
                ),
                _step(
                    "quarantine_proven_wrong_media",
                    "Quarantine the exact proven wrong media and detach only its proven registration.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
                _step(
                    "reacquire_expected_media",
                    "Select and acquire a replacement for the expected book under guarded candidate policy.",
                    external_mutation=True,
                ),
                _step(
                    "verify_replacement",
                    "Require full E2 verification of downloaded bytes before admission.",
                    stop_if_unproven=True,
                ),
                _step(
                    "admit_and_reconcile",
                    "Admit the verified replacement and confirm final registration.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )
    return (
        "RECOVER_WRONG_CONTENT",
        [
            _step(
                "revalidate_wrong_content",
                "Re-run deterministic identity checks and reproduce WRONG_CONTENT at the current source.",
                stop_if_unproven=True,
            ),
            _step(
                "resolve_recovery_scope",
                "Determine whether exact ownership correction is provable; otherwise use quarantine-first replacement.",
                stop_if_unproven=True,
            ),
            _step(
                "correct_or_quarantine",
                "Correct only a proven wrong association or quarantine only the exact proven wrong media.",
                external_mutation=True,
                stop_if_unproven=True,
            ),
            _step(
                "reacquire_expected_media",
                "Acquire expected replacement media when the intended book remains missing.",
                external_mutation=True,
            ),
            _step(
                "verify_replacement",
                "Require full E2 verification before future admission.",
                stop_if_unproven=True,
            ),
            _step(
                "admit_and_reconcile",
                "Admit verified replacement media and reconcile final Bindery state.",
                external_mutation=True,
                stop_if_unproven=True,
            ),
        ],
    )


def _definition(
    decision: dict[str, Any],
    verification: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]]] | None:
    action = str(decision.get("decision") or "")
    verification_reason = str(verification.get("reasonCode") or "")
    reason_code = verification_reason or str(decision.get("reasonCode") or "")

    if action == "would_resolve_wrong_content":
        plan_kind, steps = _wrong_content_plan(reason_code)
        return plan_kind, reason_code, steps

    if action == "would_resolve_media_mismatch":
        return (
            "RECOVER_MEDIA_KIND_MISMATCH",
            reason_code or "WRONG_MEDIA_TYPE",
            [
                _step(
                    "revalidate_media_kind",
                    "Re-detect actual media kind from current bytes and reproduce the media-kind mismatch.",
                    stop_if_unproven=True,
                ),
                _step(
                    "resolve_actual_owner",
                    "Determine whether the actual media can be mapped to another Bindery item using deterministic identity and path/byte evidence.",
                    stop_if_unproven=True,
                ),
                _step(
                    "correct_or_quarantine",
                    "Correct a proven association or quarantine only the exact mismatched media.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
                _step(
                    "reacquire_expected_media",
                    "Acquire the expected media kind if it remains missing.",
                    external_mutation=True,
                ),
                _step(
                    "verify_and_reconcile",
                    "Verify replacement bytes and reconcile final registration.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_quarantine_unsafe":
        return (
            "QUARANTINE_UNSAFE_MEDIA",
            reason_code or "UNSAFE_FILE",
            [
                _step(
                    "revalidate_unsafe_verdict",
                    "Re-run the deterministic safety/integrity check and reproduce the unsafe verdict.",
                    stop_if_unproven=True,
                ),
                _step(
                    "capture_exact_source_identity",
                    "Capture exact current path, byte identity, and provenance before mutation.",
                    stop_if_unproven=True,
                ),
                _step(
                    "quarantine_exact_media",
                    "Quarantine the exact proven unsafe media and detach only the affected association.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
                _step(
                    "reacquire_expected_media",
                    "Acquire replacement media when the expected book is no longer complete.",
                    external_mutation=True,
                ),
                _step(
                    "verify_replacement",
                    "Require full E2 verification before future admission.",
                    stop_if_unproven=True,
                ),
                _step(
                    "reconcile_final_state",
                    "Confirm the active library no longer references the quarantined unsafe bytes.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_repair_metadata":
        return (
            "REPAIR_METADATA",
            reason_code or "METADATA_REPAIR_REVIEW",
            [
                _step(
                    "revalidate_content_identity",
                    "Re-verify that content identity is still correct and only metadata requires repair.",
                    stop_if_unproven=True,
                ),
                _step(
                    "preview_metadata_change",
                    "Build and validate the exact metadata-only change.",
                    stop_if_unproven=True,
                ),
                _step(
                    "apply_metadata_repair",
                    "Apply the guarded metadata-only correction.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
                _step(
                    "verify_post_repair",
                    "Re-read the media and verify the intended metadata change without content damage.",
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_reconcile_acquisition":
        return (
            "RECONCILE_ACQUISITION",
            str(decision.get("reasonCode") or "ACQUISITION_PROGRESSABLE"),
            [
                _step(
                    "reobserve_acquisition",
                    "Re-read durable acquisition, queue, and staging state without choosing a new candidate.",
                    stop_if_unproven=True,
                ),
                _step(
                    "resume_known_transition",
                    "Resume only the previously authorized acquisition transition whose prerequisites remain true.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_review_verified_acquisition":
        return (
            "PREPARE_ACQUISITION_ADMISSION",
            str(decision.get("reasonCode") or "VERIFIED_ACQUISITION_REVIEW_AVAILABLE"),
            [
                _step(
                    "review_verified_acquisition",
                    "Review the exact durable staged verification and acquisition linkage.",
                    stop_if_unproven=True,
                ),
                _step(
                    "admit_verified_acquisition",
                    "Future separately allowlisted admission must revalidate live queue, bytes, book, destination, and readiness before publication.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_reconcile_admission":
        return (
            "RECONCILE_ADMISSION",
            str(decision.get("reasonCode") or "ADMISSION_REGISTRATION_PENDING"),
            [
                _step(
                    "reobserve_registration",
                    "Re-read admission journal and current Bindery registration.",
                    stop_if_unproven=True,
                ),
                _step(
                    "reconcile_known_admission",
                    "Resume only the existing guarded admission/reconciliation transition.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_request_published_admission_scan":
        return (
            "REQUEST_PUBLISHED_ACQUISITION_SCAN",
            "PUBLISHED_ACQUISITION_ADMISSION_SCAN_READY",
            [
                _step(
                    "reobserve_published_acquisition_admission",
                    "Verify the linked acquisition, publication receipt, bytes, and Bindery ownership.",
                    stop_if_unproven=True,
                ),
                _step(
                    "request_published_acquisition_scan",
                    "Request one Bindery scan for the exact published acquisition admission.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_correct_registration_conflict":
        return (
            "CORRECT_REGISTRATION_CONFLICT",
            str(decision.get("reasonCode") or "REGISTRATION_CONFLICT"),
            [
                _step(
                    "revalidate_registration_conflict",
                    "Re-read the durable admission, exact published/staged byte identity, current Bindery owner, queue state, import mode, and intended book identity.",
                    stop_if_unproven=True,
                ),
                _step(
                    "correct_exact_registration_owner",
                    "Run only the existing guarded no-move correction transaction: preserve library/staged bytes, never remove download-client data, and restore external import mode after exact ownership reassignment.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
                _step(
                    "verify_registration_correction",
                    "Independently prove the exact path belongs only to the intended book, external import mode is restored, auto-grab remains disabled, and both byte copies still match the verified SHA-256.",
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_finalize_acquisition":
        return (
            "FINALIZE_ACQUISITION",
            str(decision.get("reasonCode") or "FINALIZATION_RECOVERY_AVAILABLE"),
            [
                _step(
                    "revalidate_finalization_state",
                    "Re-read the durable acquisition/admission journal and prove finalization remains safe.",
                    stop_if_unproven=True,
                ),
                _step(
                    "finish_guarded_cleanup",
                    "Resume only the known finalization/cleanup operation.",
                    external_mutation=True,
                    stop_if_unproven=True,
                ),
            ],
        )

    if action == "would_recover_acquisition_failure":
        code = str(decision.get("reasonCode") or "")
        if code == "ACQUISITION_RELEASE_ALREADY_IMPORTED":
            return (
                "SELECT_ALTERNATE_REPLACEMENT",
                code,
                [
                    _step(
                        "revalidate_failed_acquisition",
                        "Re-read the failed acquisition and confirm Bindery rejected the exact selected release as already grabbed/imported.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "refresh_history_and_candidates",
                        "Refresh Bindery history and replacement search; exclude the rejected candidate and any release with equivalent imported provenance.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "select_alternate_candidate",
                        "Select a different candidate only when deterministic title/author/media policy passes and no prior imported provenance disqualifies it.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "request_alternate_grab",
                        "Request exactly the newly selected candidate after acquisition readiness is freshly revalidated.",
                        external_mutation=True,
                        stop_if_unproven=True,
                    ),
                    _step(
                        "reconcile_download_and_staging",
                        "Track the resulting queue/staging state and attribute exactly one candidate to the acquisition.",
                        external_mutation=True,
                        stop_if_unproven=True,
                    ),
                    _step(
                        "verify_downloaded_bytes",
                        "Run full content verification on the staged replacement before any admission is authorized.",
                        stop_if_unproven=True,
                    ),
                ],
            )
        if code == "ACQUISITION_TRANSIENT_BINDERY_FAILURE":
            return (
                "RETRY_ACQUISITION_TRANSIENT",
                code,
                [
                    _step(
                        "wait_bounded_backoff",
                        "Wait for the persisted bounded retry interval; do not spin or retry immediately.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "revalidate_acquisition_readiness",
                        "Freshly revalidate queue/staging readiness, book identity, and the selected candidate.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "retry_grab_once",
                        "Retry the same guarded grab only while the persisted retry budget remains.",
                        external_mutation=True,
                        stop_if_unproven=True,
                    ),
                    _step(
                        "reconcile_after_retry",
                        "Reconcile queue/staging state after the retry without weakening verification.",
                        external_mutation=True,
                        stop_if_unproven=True,
                    ),
                ],
            )

    if action == "would_recover_admission_failure":
        code = str(decision.get("reasonCode") or "")
        if code == "ADMISSION_FAILED_BEFORE_PUBLICATION":
            return (
                "REVIEW_ADMISSION_PREPUBLICATION",
                code,
                [
                    _step(
                        "review_failed_admission",
                        "Confirm the exact failed journal is marked before publication and has no publication method.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "review_current_handoff",
                        "Independently review staged bytes, absent destination, Bindery identity, and the verified acquisition before authorizing any retry.",
                        stop_if_unproven=True,
                    ),
                ],
            )
        if code == "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED":
            return (
                "RECOVER_ADMISSION_PUBLICATION",
                code,
                [
                    _step(
                        "revalidate_failed_admission",
                        "Re-read the failed admission and confirm publication never completed and the destination remains absent.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "revalidate_staged_bytes",
                        "Re-resolve the staged source and require the same verified byte identity before attempting recovery.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "revalidate_admission_topology",
                        "Re-run admission readiness and path/root mapping checks against the current filesystem topology.",
                        stop_if_unproven=True,
                    ),
                    _step(
                        "prove_supported_no_replace_method",
                        "Prove a no-overwrite publication primitive on the current filesystem without weakening atomic destination protection.",
                        external_mutation=True,
                        stop_if_unproven=True,
                    ),
                    _step(
                        "retry_guarded_publication",
                        "Retry publication only with a proven no-overwrite method and unchanged staged bytes/destination.",
                        external_mutation=True,
                        stop_if_unproven=True,
                    ),
                    _step(
                        "scan_and_reconcile_registration",
                        "Request/reconcile Bindery registration only after publication succeeds and the published bytes still verify.",
                        external_mutation=True,
                        stop_if_unproven=True,
                    ),
                ],
            )
    return None


def _build_plan(conn, decision: dict[str, Any]) -> dict[str, Any] | None:
    action = str(decision.get("decision") or "")
    if action not in _PLAN_DECISIONS:
        return None

    result_id = decision.get("resultId")
    verification = _latest_verification_context(conn, result_id)
    result = _result_context(conn, result_id)
    definition = _definition(decision, verification)
    if definition is None:
        return None
    plan_kind, reason_code, steps = definition

    decision_evidence = decision.get("evidence") or {}
    revision_evidence = _decision_evidence_for_revision(
        plan_kind,
        decision_evidence,
    )
    subject_kind = str(decision.get("subjectKind") or "")
    if subject_kind == "result" and verification.get("revision"):
        evidence_revision = str(verification["revision"])
    else:
        evidence_revision = _stable_hash(
            {
                "subjectKind": decision.get("subjectKind"),
                "subjectId": decision.get("subjectId"),
                "state": decision.get("state"),
                "decision": action,
                "reasonCode": decision.get("reasonCode"),
                "evidence": revision_evidence,
            }
        )

    required_checks = [
        "SUBJECT_IDENTITY_UNCHANGED",
        "PATH_UNCHANGED",
        "EVIDENCE_REVISION_UNCHANGED",
        "DECISION_STILL_AUTHORIZED",
    ]
    if subject_kind in {"acquisition", "admission"}:
        required_checks.extend([
            "WORKFLOW_STATE_UNCHANGED",
            "RECOVERY_CLASSIFICATION_UNCHANGED",
        ])

    recovery_classification = (
        decision_evidence.get("recoveryClassification")
        if isinstance(decision_evidence.get("recoveryClassification"), dict)
        else {}
    )

    # A pending scan may be observed again, but automatic reconciliation never
    # sends another scan. Bound the read-only checks to two hours.
    if (
        plan_kind == "RECONCILE_ADMISSION"
        and reason_code == "REGISTRATION_SCAN_PENDING"
    ):
        recovery_classification = {
            "retrySameOperation": True,
            "maxRetries": 120,
            "backoffSeconds": [60],
        }

    preconditions = {
        "requiredChecks": required_checks,
        "subject": {
            "subjectKind": str(decision.get("subjectKind") or ""),
            "subjectId": str(decision.get("subjectId") or ""),
            "resultId": result_id,
            "bookId": decision.get("bookId"),
            "scanId": str(result.get("scan_id") or decision_evidence.get("scanId") or ""),
            "fileId": result.get("file_id"),
            "format": str(result.get("format") or decision_evidence.get("format") or ""),
            "storedPath": str(result.get("stored_path") or decision.get("path") or ""),
            "localPath": str(result.get("local_path") or ""),
        },
        "verification": {
            "id": verification.get("id"),
            "signature": str(verification.get("signature") or ""),
            "verdict": str(verification.get("verdict") or ""),
            "confidence": int(verification.get("confidence") or 0),
            "source": str(verification.get("source") or ""),
            "reasonCode": str(verification.get("reasonCode") or ""),
            "revision": evidence_revision,
        },
        "retryPolicy": {
            "retrySameOperation": bool(
                recovery_classification.get("retrySameOperation", False)
            ),
            "maxRetries": int(recovery_classification.get("maxRetries") or 0),
            "backoffSeconds": list(
                recovery_classification.get("backoffSeconds") or []
            ),
        },
        "mutationBoundary": (
            "Every external mutation step must freshly revalidate these checks. "
            "Any changed path, bytes/evidence revision, workflow state, Bindery "
            "owner, or mapping invalidates the plan and forces re-planning."
        ),
    }

    plan = {
        "plannerVersion": PLANNER_VERSION,
        "subjectKind": str(decision.get("subjectKind") or ""),
        "subjectId": str(decision.get("subjectId") or ""),
        "resultId": result_id,
        "bookId": decision.get("bookId"),
        "title": str(decision.get("title") or ""),
        "author": str(decision.get("author") or ""),
        "path": str(decision.get("path") or ""),
        "planKind": plan_kind,
        "reasonCode": reason_code,
        "state": "planned",
        "evidenceRevision": evidence_revision,
        "preconditions": preconditions,
        "steps": steps,
        "currentStep": 0,
        "retryCount": 0,
        "nextRetryAt": None,
        "lastError": "",
        "executionAllowed": False,
    }
    plan["signature"] = _stable_hash(
        {
            "plannerVersion": PLANNER_VERSION,
            "subjectKind": plan["subjectKind"],
            "subjectId": plan["subjectId"],
            "planKind": plan_kind,
            "reasonCode": reason_code,
            "evidenceRevision": evidence_revision,
            "preconditions": preconditions,
            "steps": steps,
        }
    )
    return plan


def _decode_plan_row(row) -> dict[str, Any]:
    item = dict(row)
    preconditions = _json_object(item.pop("preconditions_json", "{}"))
    try:
        steps = json.loads(str(item.pop("steps_json", "[]")))
    except json.JSONDecodeError:
        steps = []
    if not isinstance(steps, list):
        steps = []
    return {
        "id": item["id"],
        "signature": item["signature"],
        "plannerVersion": item["planner_version"],
        "subjectKind": item["subject_kind"],
        "subjectId": item["subject_id"],
        "resultId": item["result_id"],
        "bookId": item["book_id"],
        "title": item["title"],
        "author": item["author"],
        "path": item["path"],
        "planKind": item["plan_kind"],
        "reasonCode": item["reason_code"],
        "state": item["state"],
        "evidenceRevision": item["evidence_revision"],
        "preconditions": preconditions,
        "steps": steps,
        "currentStep": item["current_step"],
        "retryCount": item["retry_count"],
        "nextRetryAt": item["next_retry_at"],
        "lastError": str(item["last_error"] or ""),
        "createdAt": item["created_at"],
        "updatedAt": item["updated_at"],
        "completedAt": item["completed_at"],
        "finalOutcome": str(item.get("final_outcome") or ""),
        "executionAllowed": False,
    }


def _record_transition(
    conn,
    *,
    plan_id: int,
    event: str,
    from_state: str,
    to_state: str,
    from_step: int,
    to_step: int,
    detail: str,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO recovery_plan_transitions(
            plan_id, event, from_state, to_state, from_step, to_step,
            detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(plan_id),
            str(event),
            str(from_state),
            str(to_state),
            int(from_step),
            int(to_step),
            str(detail or ""),
            str(now),
        ),
    )


def recovery_plan_transition_snapshot(plan_id: int) -> list[dict[str, Any]]:
    with local_conn() as conn:
        if not _table_exists(conn, "recovery_plan_transitions"):
            return []
        rows = conn.execute(
            """
            SELECT id, plan_id, event, from_state, to_state,
                   from_step, to_step, detail, created_at
            FROM recovery_plan_transitions
            WHERE plan_id=?
            ORDER BY id
            """,
            (int(plan_id),),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "planId": int(row["plan_id"]),
            "event": str(row["event"]),
            "fromState": str(row["from_state"]),
            "toState": str(row["to_state"]),
            "fromStep": int(row["from_step"]),
            "toStep": int(row["to_step"]),
            "detail": str(row["detail"] or ""),
            "createdAt": str(row["created_at"]),
        }
        for row in rows
    ]


def _supersede_active(conn, subject_kind: str, subject_id: str, now: str, reason: str) -> None:
    placeholders = ",".join("?" for _ in _PLAN_STATES_ACTIVE)
    rows = conn.execute(
        f"""
        SELECT id, state, current_step
        FROM recovery_plans
        WHERE subject_kind=? AND subject_id=?
          AND state IN ({placeholders})
        """,
        (subject_kind, subject_id, *sorted(_PLAN_STATES_ACTIVE)),
    ).fetchall()
    for row in rows:
        _record_transition(
            conn,
            plan_id=int(row["id"]),
            event="superseded",
            from_state=str(row["state"]),
            to_state="superseded",
            from_step=int(row["current_step"]),
            to_step=int(row["current_step"]),
            detail=reason,
            now=now,
        )
    conn.execute(
        f"""
        UPDATE recovery_plans
        SET state='superseded', updated_at=?, last_error=?,
            final_outcome='superseded'
        WHERE subject_kind=? AND subject_id=?
          AND state IN ({placeholders})
        """,
        (
            now,
            reason,
            subject_kind,
            subject_id,
            *sorted(_PLAN_STATES_ACTIVE),
        ),
    )


def _persist_plan(conn, plan: dict[str, Any], now: str) -> dict[str, Any]:
    subject_kind = str(plan["subjectKind"])
    subject_id = str(plan["subjectId"])
    signature = str(plan["signature"])
    existing = conn.execute(
        "SELECT * FROM recovery_plans WHERE signature=? LIMIT 1",
        (signature,),
    ).fetchone()

    placeholders = ",".join("?" for _ in _PLAN_STATES_ACTIVE)
    replaced_rows = conn.execute(
        f"""
        SELECT id, state, current_step
        FROM recovery_plans
        WHERE subject_kind=? AND subject_id=? AND signature<>?
          AND state IN ({placeholders})
        """,
        (
            subject_kind,
            subject_id,
            signature,
            *sorted(_PLAN_STATES_ACTIVE),
        ),
    ).fetchall()
    replacement_reason = "A newer evidence revision produced a replacement recovery plan."
    for replaced in replaced_rows:
        _record_transition(
            conn,
            plan_id=int(replaced["id"]),
            event="superseded",
            from_state=str(replaced["state"]),
            to_state="superseded",
            from_step=int(replaced["current_step"]),
            to_step=int(replaced["current_step"]),
            detail=replacement_reason,
            now=now,
        )
    conn.execute(
        f"""
        UPDATE recovery_plans
        SET state='superseded', updated_at=?, last_error=?,
            final_outcome='superseded'
        WHERE subject_kind=? AND subject_id=? AND signature<>?
          AND state IN ({placeholders})
        """,
        (
            now,
            replacement_reason,
            subject_kind,
            subject_id,
            signature,
            *sorted(_PLAN_STATES_ACTIVE),
        ),
    )

    conn.execute(
        """
        INSERT INTO recovery_plans(
            signature, planner_version, subject_kind, subject_id, result_id,
            book_id, title, author, path, plan_kind, reason_code, state,
            evidence_revision, preconditions_json, steps_json, current_step,
            retry_count, next_retry_at, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, 0, 0, NULL, NULL, ?, ?)
        ON CONFLICT(signature) DO UPDATE SET
            title=excluded.title,
            author=excluded.author,
            path=excluded.path,
            state=CASE
                WHEN recovery_plans.state='superseded' THEN 'planned'
                ELSE recovery_plans.state
            END,
            preconditions_json=excluded.preconditions_json,
            steps_json=excluded.steps_json,
            updated_at=excluded.updated_at,
            last_error=CASE
                WHEN recovery_plans.state='superseded' THEN NULL
                ELSE recovery_plans.last_error
            END,
            completed_at=CASE
                WHEN recovery_plans.state='superseded' THEN NULL
                ELSE recovery_plans.completed_at
            END,
            final_outcome=CASE
                WHEN recovery_plans.state='superseded' THEN NULL
                ELSE recovery_plans.final_outcome
            END
        """,
        (
            signature,
            PLANNER_VERSION,
            subject_kind,
            subject_id,
            plan.get("resultId"),
            plan.get("bookId"),
            plan.get("title") or "",
            plan.get("author") or "",
            plan.get("path") or "",
            plan["planKind"],
            plan["reasonCode"],
            plan["evidenceRevision"],
            json.dumps(plan["preconditions"], ensure_ascii=False, sort_keys=True),
            json.dumps(plan["steps"], ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM recovery_plans WHERE signature=? LIMIT 1",
        (signature,),
    ).fetchone()
    if existing is None:
        _record_transition(
            conn,
            plan_id=int(row["id"]),
            event="created",
            from_state="",
            to_state=str(row["state"]),
            from_step=0,
            to_step=int(row["current_step"]),
            detail="Recovery plan created from the current Observe decision.",
            now=now,
        )
    elif str(existing["state"]) == "superseded" and str(row["state"]) == "planned":
        _record_transition(
            conn,
            plan_id=int(row["id"]),
            event="reactivated",
            from_state="superseded",
            to_state="planned",
            from_step=int(existing["current_step"]),
            to_step=int(row["current_step"]),
            detail="The exact evidence revision became current again.",
            now=now,
        )
    return _decode_plan_row(row)


def record_recovery_plans(
    conn,
    decisions: list[dict[str, Any]],
    *,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Persist non-executable E3 recovery plans derived from Observe decisions.

    This function mutates only BookGuard's local recovery-plan journal. It never
    contacts Bindery, touches queue/staging state, or changes library bytes.
    """
    if not _table_exists(conn, "recovery_plans"):
        raise RuntimeError(
            "Recovery plan journal is unavailable. Restart BookGuard so database "
            "initialization can complete."
        )

    timestamp = now or utc_now()
    plans: list[dict[str, Any]] = []
    for decision in decisions:
        subject_kind = str(decision.get("subjectKind") or "")
        subject_id = str(decision.get("subjectId") or "")
        plan = _build_plan(conn, decision)
        if plan is None:
            _supersede_active(
                conn,
                subject_kind,
                subject_id,
                timestamp,
                "Current Observe decision no longer authorizes automatic recovery.",
            )
            continue
        plans.append(_persist_plan(conn, plan, timestamp))
    return plans


def recovery_plan_by_id(plan_id: int) -> dict[str, Any] | None:
    with local_conn() as conn:
        if not _table_exists(conn, "recovery_plans"):
            return None
        row = conn.execute(
            "SELECT * FROM recovery_plans WHERE id=? LIMIT 1",
            (int(plan_id),),
        ).fetchone()
    return _decode_plan_row(row) if row else None


def _coerce_utc(value: str | None) -> datetime:
    if value:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def record_recovery_step_success(
    plan_id: int,
    expected_step: int,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Advance only the local recovery journal after one step is reported complete.

    This function performs no recovery step itself. E4 may call it only after the
    executor has completed and revalidated the corresponding step.
    """
    timestamp = _coerce_utc(now).isoformat()
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM recovery_plans WHERE id=? LIMIT 1",
            (int(plan_id),),
        ).fetchone()
        if row is None:
            raise RuntimeError("Recovery plan not found.")
        plan = _decode_plan_row(row)
        steps = list(plan["steps"])
        current_step = int(plan["currentStep"] or 0)
        requested = int(expected_step)

        if requested < current_step:
            # Idempotent replay after restart: already recorded.
            return plan
        if requested != current_step:
            raise RuntimeError(
                f"Recovery plan expects step {current_step}; received {requested}."
            )
        if requested >= len(steps):
            raise RuntimeError("Recovery plan has no remaining step to complete.")
        if plan["state"] in {"blocked", "superseded", "completed"}:
            raise RuntimeError(
                f"Recovery plan state '{plan['state']}' cannot advance."
            )

        next_step = current_step + 1
        complete = next_step >= len(steps)
        next_state = "completed" if complete else "ready"
        step_code = str((steps[current_step] or {}).get("code") or current_step)
        conn.execute(
            """
            UPDATE recovery_plans
            SET current_step=?, state=?, updated_at=?,
                completed_at=?, final_outcome=?,
                next_retry_at=NULL, last_error=NULL
            WHERE id=?
            """,
            (
                next_step,
                next_state,
                timestamp,
                timestamp if complete else None,
                "completed" if complete else None,
                int(plan_id),
            ),
        )
        _record_transition(
            conn,
            plan_id=int(plan_id),
            event="step_completed",
            from_state=str(plan["state"]),
            to_state=next_state,
            from_step=current_step,
            to_step=next_step,
            detail=f"Recorded completion of step '{step_code}'.",
            now=timestamp,
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM recovery_plans WHERE id=? LIMIT 1",
            (int(plan_id),),
        ).fetchone()
        return _decode_plan_row(updated)


def block_recovery_plan(
    plan_id: int,
    error: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Block only one plan in the local journal; no external action is executed."""
    timestamp = _coerce_utc(now).isoformat()
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM recovery_plans WHERE id=? LIMIT 1",
            (int(plan_id),),
        ).fetchone()
        if row is None:
            raise RuntimeError("Recovery plan not found.")
        plan = _decode_plan_row(row)
        if plan["state"] in {"superseded", "completed"}:
            raise RuntimeError(
                f"Recovery plan state '{plan['state']}' cannot be blocked."
            )
        conn.execute(
            """
            UPDATE recovery_plans
            SET state='blocked', next_retry_at=NULL, last_error=?,
                updated_at=?, final_outcome='blocked'
            WHERE id=?
            """,
            (str(error or ""), timestamp, int(plan_id)),
        )
        _record_transition(
            conn,
            plan_id=int(plan_id),
            event="blocked",
            from_state=str(plan["state"]),
            to_state="blocked",
            from_step=int(plan["currentStep"] or 0),
            to_step=int(plan["currentStep"] or 0),
            detail=str(error or ""),
            now=timestamp,
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM recovery_plans WHERE id=? LIMIT 1",
            (int(plan_id),),
        ).fetchone()
        return _decode_plan_row(updated)


def schedule_recovery_retry(
    plan_id: int,
    error: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist one bounded retry opportunity without executing external work.

    E3 uses this only as durable state-machine bookkeeping. Scheduling a retry
    never contacts Bindery, reads queue/staging state, or mutates library bytes.
    """
    timestamp = _coerce_utc(now)
    now_text = timestamp.isoformat()

    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM recovery_plans WHERE id=? LIMIT 1",
            (int(plan_id),),
        ).fetchone()
        if row is None:
            raise RuntimeError("Recovery plan not found.")

        plan = _decode_plan_row(row)
        if plan["state"] not in {"planned", "ready", "retry_wait"}:
            raise RuntimeError(
                f"Recovery plan state '{plan['state']}' is not retry-schedulable."
            )

        retry_policy = plan["preconditions"].get("retryPolicy") or {}
        retry_same = bool(retry_policy.get("retrySameOperation", False))
        max_retries = int(retry_policy.get("maxRetries") or 0)
        backoff = [
            int(value)
            for value in (retry_policy.get("backoffSeconds") or [])
            if int(value) >= 0
        ]
        if not retry_same or max_retries <= 0 or not backoff:
            raise RuntimeError("Recovery plan does not authorize same-operation retries.")

        retry_count = int(plan["retryCount"] or 0)
        if retry_count >= max_retries:
            detail = (
                f"Retry budget exhausted after {retry_count} bounded retries. "
                f"Last failure: {error}"
            )
            conn.execute(
                """
                UPDATE recovery_plans
                SET state='blocked', next_retry_at=NULL, last_error=?, updated_at=?,
                    final_outcome='retry_budget_exhausted'
                WHERE id=?
                """,
                (detail, now_text, int(plan_id)),
            )
            _record_transition(
                conn,
                plan_id=int(plan_id),
                event="retry_budget_exhausted",
                from_state=str(plan["state"]),
                to_state="blocked",
                from_step=int(plan["currentStep"] or 0),
                to_step=int(plan["currentStep"] or 0),
                detail=detail,
                now=now_text,
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM recovery_plans WHERE id=? LIMIT 1",
                (int(plan_id),),
            ).fetchone()
            return _decode_plan_row(updated)

        delay_index = min(retry_count, len(backoff) - 1)
        delay_seconds = backoff[delay_index]
        next_retry = (timestamp + timedelta(seconds=delay_seconds)).isoformat()
        conn.execute(
            """
            UPDATE recovery_plans
            SET state='retry_wait',
                retry_count=?,
                next_retry_at=?,
                last_error=?,
                updated_at=?,
                final_outcome=NULL
            WHERE id=?
            """,
            (
                retry_count + 1,
                next_retry,
                str(error or ""),
                now_text,
                int(plan_id),
            ),
        )
        _record_transition(
            conn,
            plan_id=int(plan_id),
            event="retry_scheduled",
            from_state=str(plan["state"]),
            to_state="retry_wait",
            from_step=int(plan["currentStep"] or 0),
            to_step=int(plan["currentStep"] or 0),
            detail=f"Retry {retry_count + 1}/{max_retries} scheduled after {delay_seconds}s.",
            now=now_text,
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM recovery_plans WHERE id=? LIMIT 1",
            (int(plan_id),),
        ).fetchone()
        return _decode_plan_row(updated)


def promote_due_recovery_retries(*, now: str | None = None) -> list[dict[str, Any]]:
    """Move due retry-wait plans to ready without executing the retry."""
    timestamp = _coerce_utc(now)
    now_text = timestamp.isoformat()
    with local_conn() as conn:
        if not _table_exists(conn, "recovery_plans"):
            return []
        rows = conn.execute(
            """
            SELECT *
            FROM recovery_plans
            WHERE state='retry_wait'
              AND next_retry_at IS NOT NULL
              AND next_retry_at <= ?
            ORDER BY next_retry_at, id
            """,
            (now_text,),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE recovery_plans
                SET state='ready', next_retry_at=NULL, updated_at=?
                WHERE id IN ({placeholders})
                """,
                (now_text, *ids),
            )
            for row in rows:
                _record_transition(
                    conn,
                    plan_id=int(row["id"]),
                    event="retry_due",
                    from_state="retry_wait",
                    to_state="ready",
                    from_step=int(row["current_step"]),
                    to_step=int(row["current_step"]),
                    detail="Persisted retry backoff elapsed; plan is ready for revalidation.",
                    now=now_text,
                )
            conn.commit()
            promoted = conn.execute(
                f"""
                SELECT *
                FROM recovery_plans
                WHERE id IN ({placeholders})
                ORDER BY id
                """,
                ids,
            ).fetchall()
        else:
            promoted = []
    return [_decode_plan_row(row) for row in promoted]


def recovery_plan_snapshot(
    limit: int = 100,
    *,
    state: str | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 1000))
    params: list[Any] = []
    where = ""
    if state:
        where = "WHERE state=?"
        params.append(str(state).casefold())
    params.append(limit)
    with local_conn() as conn:
        if not _table_exists(conn, "recovery_plans"):
            rows = []
        else:
            rows = conn.execute(
                f"""
                SELECT *
                FROM recovery_plans
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
    items = [_decode_plan_row(row) for row in rows]
    return {
        "plannerVersion": PLANNER_VERSION,
        "count": len(items),
        "items": items,
    }
