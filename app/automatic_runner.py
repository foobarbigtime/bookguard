from __future__ import annotations

from typing import Any

from . import automatic_execution as core
from .automatic_alternate import (
    register_executor as register_alternate_grab_executor,
    run_alternate_grab_cycle,
)
from .automatic_quarantine_grab import (
    register_executor as register_quarantine_grab_executor,
    run_quarantine_grab_cycle,
)
from .acquisition_progress import (
    register_executor as register_acquisition_progress_executor,
    run_acquisition_progress_cycle,
)
from .acquisition_admission_execution import (
    register_executor as register_verified_admission_executor,
    run_verified_admission_cycle,
)
from .acquisition_admission_scan import (
    register_executor as register_acquisition_scan_executor,
    run_published_acquisition_scan_cycle,
)
from .acquisition import AcquisitionSafetyError, finalize_ebook_acquisition
from .config import load_automation_settings
from .db import ebook_acquisition_by_id, local_conn
from .quarantine_selection import quarantine_selection_by_result
from .finalization import finalization_preview
from .observe import _acquisition_decisions
from .publication_proof import register_executor as register_publication_proof_executor
from .publication_recovery import (
    register_executor as register_guarded_publication_executor,
    run_publication_recovery_cycle,
)
from .publication_registration import (
    register_executor as register_publication_scan_executor,
    run_publication_registration_cycle,
)
from .prepublication_retirement import (
    register_executor as register_prepublication_retirement_executor,
    run_prepublication_retirement_cycle,
)
from .recovery_planner import _build_plan
from .registration_correction import (
    register_executor as register_registration_conflict_executor,
    run_registration_conflict_cycle,
)


AutomaticExecutionBlocked = core.AutomaticExecutionBlocked
automatic_execution_history = core.automatic_execution_history
register_registration_conflict_executor()
register_acquisition_progress_executor()
register_verified_admission_executor()
register_acquisition_scan_executor()
register_publication_proof_executor()
register_guarded_publication_executor()
register_publication_scan_executor()
register_alternate_grab_executor()
register_quarantine_grab_executor()
register_prepublication_retirement_executor()


class _FinalizationCleanupExecutor:
    action_code = "finish_guarded_cleanup"

    def _current_plan(self, acquisition_id: int) -> dict[str, Any] | None:
        with local_conn() as conn:
            decisions = _acquisition_decisions(conn, 500)
            current_decision = next(
                (
                    item
                    for item in decisions
                    if str(item.get("subjectKind") or "") == "acquisition"
                    and str(item.get("subjectId") or "") == str(acquisition_id)
                ),
                None,
            )
            return _build_plan(conn, current_decision) if current_decision else None

    def revalidate(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
    ) -> dict[str, Any]:
        if str(plan.get("planKind") or "") != "FINALIZE_ACQUISITION":
            raise AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH",
                "finish_guarded_cleanup is valid only for FINALIZE_ACQUISITION.",
            )
        if str(plan.get("subjectKind") or "") != "acquisition":
            raise AutomaticExecutionBlocked(
                "SUBJECT_KIND_MISMATCH",
                "Guarded finalization requires an acquisition subject.",
            )
        if str(plan.get("reasonCode") or "") != "FINALIZATION_RECOVERY_AVAILABLE":
            raise AutomaticExecutionBlocked(
                "REASON_CODE_MISMATCH",
                "Live finalization is limited to FINALIZATION_RECOVERY_AVAILABLE.",
            )

        acquisition_id = int(plan["subjectId"])
        if not ebook_acquisition_by_id(acquisition_id):
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The planned acquisition no longer exists.",
            )

        current_plan = self._current_plan(acquisition_id)
        if not current_plan:
            raise AutomaticExecutionBlocked(
                "DECISION_NO_LONGER_AUTHORIZED",
                "The current decision policy no longer authorizes finalization.",
            )

        try:
            preview = finalization_preview(acquisition_id)
        except AcquisitionSafetyError as exc:
            raise AutomaticExecutionBlocked(
                "FINALIZATION_PREFLIGHT_FAILED",
                str(exc),
            ) from exc

        preview_checks = dict(preview.get("checks") or {})
        checks = [
            {
                "code": "SUBJECT_IDENTITY_UNCHANGED",
                "ok": (
                    str(current_plan.get("subjectId") or "") == str(plan.get("subjectId") or "")
                    and current_plan.get("resultId") == plan.get("resultId")
                    and current_plan.get("bookId") == plan.get("bookId")
                    and int(preview.get("acquisitionId") or 0) == acquisition_id
                ),
            },
            {
                "code": "PATH_UNCHANGED",
                "ok": str(current_plan.get("path") or "") == str(plan.get("path") or ""),
            },
            {
                "code": "EVIDENCE_REVISION_UNCHANGED",
                "ok": str(current_plan.get("evidenceRevision") or "")
                == str(plan.get("evidenceRevision") or ""),
            },
            {
                "code": "DECISION_STILL_AUTHORIZED",
                "ok": str(current_plan.get("signature") or "")
                == str(plan.get("signature") or ""),
            },
            {
                "code": "WORKFLOW_STATE_FINALIZABLE",
                "ok": bool(preview_checks.get("workflowStateFinalizable")),
            },
            {
                "code": "LINKED_ADMISSION_REGISTERED",
                "ok": bool(preview_checks.get("linkedAdmissionRegistered")),
            },
            {
                "code": "ACQUISITION_ADMISSION_IDENTITY_CURRENT",
                "ok": bool(preview_checks.get("acquisitionAdmissionIdentityConsistent")),
            },
            {
                "code": "RESULT_IDENTITY_CURRENT",
                "ok": bool(preview_checks.get("resultIdentityConsistent")),
            },
            {
                "code": "LIBRARY_BYTES_CURRENT",
                "ok": bool(preview_checks.get("libraryBytesCurrent")),
            },
            {
                "code": "BINDERY_REGISTRATION_CURRENT",
                "ok": bool(preview_checks.get("binderyRegistrationCurrent")),
            },
            {
                "code": "QUEUE_RESPONSE_COMPLETE",
                "ok": bool(preview_checks.get("queueResponseComplete")),
            },
            {
                "code": "QUEUE_IDENTITY_CURRENT",
                "ok": all((
                    bool(preview_checks.get("queueIdPresent")),
                    bool(preview_checks.get("queueIdentityUnambiguous")),
                    bool(preview_checks.get("queueIdentityCurrent")),
                    bool(preview_checks.get("queueStatusFinalizable")),
                )),
            },
            {
                "code": "STAGING_STATE_CURRENT",
                "ok": bool(preview_checks.get("stagingStateSafe")),
            },
            {
                "code": "CLEANUP_STATE_CONSISTENT",
                "ok": bool(preview_checks.get("cleanupStateConsistent")),
            },
            {
                "code": "ACTIONS_ENABLED",
                "ok": bool(preview_checks.get("actionsEnabled")),
            },
            {
                "code": "AUTOMATIC_REACQUISITION_ENABLED",
                "ok": bool(preview_checks.get("automaticReacquisitionEnabled")),
            },
        ]

        return {
            "ok": all(check["ok"] for check in checks),
            "planSignature": str(plan["signature"]),
            "evidenceRevision": str(plan["evidenceRevision"]),
            "checks": checks,
            "acquisitionId": acquisition_id,
            "admissionId": preview.get("admissionId"),
            "resultId": int(preview["resultId"]),
            "bookId": int(preview["bookId"]),
            "stagedRelativePath": str(preview.get("stagedRelativePath") or ""),
            "stagedSha256": str(preview.get("stagedSha256") or ""),
            "queueId": preview.get("queueId"),
            "queuePresent": bool(preview.get("queuePresent")),
            "stagedPresent": bool(preview.get("stagedPresent")),
            "cleanupState": str(preview.get("cleanupState") or ""),
        }

    @staticmethod
    def _outcome(
        acquisition_id: int,
        cleanup_state: str,
        response: dict[str, Any],
        *,
        reconciled_after_restart: bool = False,
    ) -> dict[str, Any]:
        acquisition = response.get("acquisition") or {}
        status = str(acquisition.get("status") or "")
        if status != "finalized":
            raise RuntimeError(
                "Guarded finalization did not reach durable finalized state."
            )
        mutation_performed = cleanup_state in {
            "queue_and_staging_pending",
            "staging_pending",
        }
        return {
            "acquisitionId": acquisition_id,
            "status": status,
            "cleanupStateBefore": cleanup_state,
            "queueMutationPerformed": cleanup_state == "queue_and_staging_pending",
            "stagedMutationPerformed": mutation_performed,
            "externalMutationPerformed": mutation_performed,
            "removedFromDownloadClient": bool(
                response.get("removedFromDownloadClient", False)
            ),
            "downloadedDataDeleted": bool(response.get("downloadedDataDeleted", False)),
            "libraryBytesChanged": False,
            "cleanupAlreadyComplete": bool(response.get("cleanupAlreadyComplete", False)),
            "reconciledAfterRestart": reconciled_after_restart,
        }

    def execute(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any]:
        acquisition_id = int(plan["subjectId"])
        cleanup_state = str(boundary.get("cleanupState") or "")
        try:
            response = finalize_ebook_acquisition(acquisition_id)
        except AcquisitionSafetyError as exc:
            raise RuntimeError(str(exc)) from exc
        return self._outcome(acquisition_id, cleanup_state, response)

    def reconcile_uncertain(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        acquisition_id = int(plan["subjectId"])
        if not ebook_acquisition_by_id(acquisition_id):
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The interrupted acquisition no longer exists.",
            )

        prior = dict(existing.get("boundary") or {})
        if "queuePresent" not in prior or "stagedPresent" not in prior:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted finalization lacks the prior queue/staging boundary proof.",
            )

        try:
            preview = finalization_preview(acquisition_id)
        except AcquisitionSafetyError as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Interrupted finalization could not be proven safely: {exc}",
            ) from exc
        if preview.get("safe") is not True:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted finalization no longer satisfies every cleanup invariant.",
            )

        previous_queue = bool(prior.get("queuePresent"))
        previous_staged = bool(prior.get("stagedPresent"))
        current_queue = bool(preview.get("queuePresent"))
        current_staged = bool(preview.get("stagedPresent"))
        if not previous_queue and current_queue:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "The finalized queue record reappeared after the prior boundary; replay is blocked.",
            )
        if not previous_staged and current_staged:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "The staged source reappeared after the prior boundary; replay is blocked.",
            )

        cleanup_state = str(preview.get("cleanupState") or "")
        if cleanup_state not in {
            "queue_and_staging_pending",
            "staging_pending",
            "complete",
        }:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted finalization is not in a provably monotonic cleanup state.",
            )

        try:
            response = finalize_ebook_acquisition(acquisition_id)
        except AcquisitionSafetyError as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Proven remaining cleanup could not be resumed safely: {exc}",
            ) from exc
        return self._outcome(
            acquisition_id,
            cleanup_state,
            response,
            reconciled_after_restart=True,
        )


_FINALIZATION_EXECUTOR = _FinalizationCleanupExecutor()
core.register_automatic_executor("finish_guarded_cleanup", _FINALIZATION_EXECUTOR)


def execution_policy_snapshot() -> dict[str, Any]:
    """Return the core policy after this E4 slice registers finalization."""
    return core.execution_policy_snapshot()


def _run_finalization_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = int(plan["id"])
    refreshed = core.recovery_plan_by_id(plan_id) or plan
    steps = list(refreshed.get("steps") or [])
    index = int(refreshed.get("currentStep") or 0)
    code = (
        str((steps[index] or {}).get("code") or "")
        if 0 <= index < len(steps)
        else ""
    )

    if code == "revalidate_finalization_state":
        try:
            boundary = _FINALIZATION_EXECUTOR.revalidate(refreshed, steps[index])
            core._require_fresh_boundary(refreshed, boundary)
        except AutomaticExecutionBlocked as exc:
            blocked = core.block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False,
                "state": "blocked",
                "plan": blocked,
                "externalMutationAttempted": False,
                "reasonCode": exc.reason_code,
                "message": str(exc),
            }
        refreshed = core.record_recovery_step_success(plan_id, index)

    refreshed = core.recovery_plan_by_id(plan_id) or refreshed
    steps = list(refreshed.get("steps") or [])
    index = int(refreshed.get("currentStep") or 0)
    code = (
        str((steps[index] or {}).get("code") or "")
        if 0 <= index < len(steps)
        else ""
    )
    if code != "finish_guarded_cleanup":
        return {
            "ok": True,
            "state": "paused",
            "plan": refreshed,
            "externalMutationAttempted": False,
            "message": "The guarded finalization step is complete or no longer current.",
        }

    try:
        result = core.attempt_automatic_step(plan_id)
    except AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(
            plan_id,
            "Guarded finalization stopped safely: " + str(exc),
        )
        return {
            "ok": False,
            "state": "blocked",
            "plan": blocked,
            "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED",
            "reasonCode": exc.reason_code,
            "message": str(exc),
        }

    replayed = bool(result.get("replayed"))
    reconciled = bool(result.get("reconciled"))
    external_result = dict((result.get("execution") or {}).get("externalResult") or {})
    return {
        **result,
        "state": "reconciled" if reconciled else "replayed" if replayed else "executed",
        "externalMutationAttempted": bool(
            external_result.get("externalMutationPerformed", not replayed)
        ),
    }


def run_automatic_cycle(limit: int = 100) -> dict[str, Any]:
    """Advance one ready E4 item while leaving incomplete handoffs waiting."""
    configured = load_automation_settings()
    if configured.automation_mode != "automatic":
        raise AutomaticExecutionBlocked(
            "AUTOMATIC_MODE_INACTIVE",
            "BOOKGUARD_AUTOMATION_MODE must be automatic.",
        )

    snapshot = core.recovery_plan_snapshot(limit)
    candidates = [
        item
        for item in snapshot["items"]
        if (
            item.get("planKind")
            in {
                "RETRY_ACQUISITION_TRANSIENT",
                "QUARANTINE_UNSAFE_MEDIA",
                "CORRECT_REGISTRATION_CONFLICT",
                "FINALIZE_ACQUISITION",
                "RECONCILE_ACQUISITION",
                "PREPARE_ACQUISITION_ADMISSION",
                "REVIEW_ADMISSION_PREPUBLICATION",
                "REQUEST_PUBLISHED_ACQUISITION_SCAN",
                "RECOVER_ADMISSION_PUBLICATION",
                "SELECT_ALTERNATE_REPLACEMENT",
            }
            or (
                item.get("planKind") == "RECONCILE_ADMISSION"
                and item.get("subjectKind") == "admission"
            )
        )
        and (
            item.get("planKind") != "PREPARE_ACQUISITION_ADMISSION"
            or "admit_verified_acquisition" in configured.automatic_action_allowlist
        )
        and (
            item.get("planKind") != "REVIEW_ADMISSION_PREPUBLICATION"
            or "retire_proven_prepublication_failure" in configured.automatic_action_allowlist
        )
        and (
            item.get("planKind") != "REQUEST_PUBLISHED_ACQUISITION_SCAN"
            or "request_published_acquisition_scan" in configured.automatic_action_allowlist
        )
        and item.get("state") in {"planned", "ready", "retry_wait"}
    ]
    candidates.sort(key=lambda item: int(item["id"]))

    first_waiting = None
    first_paused = None
    has_retry_wait = False
    for candidate in candidates:
        kind = str(candidate.get("planKind") or "")
        if candidate.get("state") == "retry_wait":
            has_retry_wait = True
            continue
        if (
            kind == "QUARANTINE_UNSAFE_MEDIA"
            and int(candidate.get("currentStep") or 0) == 3
            and "reacquire_expected_media" in getattr(
                configured, "automatic_action_allowlist", ()
            )
            and quarantine_selection_by_result(int(candidate["subjectId"])) is not None
        ):
            result = run_quarantine_grab_cycle(candidate)
            if result.get("externalMutationAttempted") is False and result.get("state") == "waiting":
                if first_waiting is None:
                    first_waiting = result
                continue
            return result
        if core.quarantine_plan_paused(candidate):
            if first_paused is None:
                first_paused = core.paused_quarantine_result(candidate)
            continue
        if kind == "RECONCILE_ACQUISITION":
            result = run_acquisition_progress_cycle(candidate)
        elif kind == "PREPARE_ACQUISITION_ADMISSION":
            result = run_verified_admission_cycle(candidate)
        elif kind == "REVIEW_ADMISSION_PREPUBLICATION":
            result = run_prepublication_retirement_cycle(candidate)
        elif kind == "REQUEST_PUBLISHED_ACQUISITION_SCAN":
            result = run_published_acquisition_scan_cycle(candidate)
        elif kind == "SELECT_ALTERNATE_REPLACEMENT":
            result = run_alternate_grab_cycle(candidate)
        elif kind == "CORRECT_REGISTRATION_CONFLICT":
            result = run_registration_conflict_cycle(candidate)
        elif kind == "RECOVER_ADMISSION_PUBLICATION":
            if int(candidate.get("currentStep") or 0) == 5:
                result = run_publication_registration_cycle(candidate)
            else:
                result = run_publication_recovery_cycle(candidate)
        elif kind == "FINALIZE_ACQUISITION":
            result = _run_finalization_cycle(candidate)
        else:
            # The core runner promotes due retries before selecting the earliest
            # executable core plan; waiting plans do not block ready work.
            result = core.run_automatic_cycle(limit)

        # Only a definite no-mutation handoff can give the slot to another item.
        # An uncertain post-effect result must stop the cycle, even if it waits.
        if result.get("externalMutationAttempted") is False:
            if result.get("state") == "waiting":
                if first_waiting is None:
                    first_waiting = result
                continue
            if result.get("state") == "paused":
                if first_paused is None:
                    first_paused = result
                continue
        return result
    if has_retry_wait:
        # A retry may have become due since the outer snapshot. The core
        # runner promotes it before returning a waiting or executed result.
        return core.run_automatic_cycle(limit)
    if first_waiting is not None:
        return first_waiting
    if first_paused is not None:
        return first_paused
    return core.run_automatic_cycle(limit)
