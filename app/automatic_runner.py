from __future__ import annotations

from typing import Any

from . import automatic_execution as core
from .acquisition import AcquisitionSafetyError, finalize_ebook_acquisition
from .config import load_automation_settings
from .db import ebook_acquisition_by_id, local_conn
from .finalization import finalization_preview
from .observe import _acquisition_decisions
from .recovery_planner import _build_plan
from .registration_correction import (
    register_executor as register_registration_conflict_executor,
    run_registration_conflict_cycle,
)


AutomaticExecutionBlocked = core.AutomaticExecutionBlocked
automatic_execution_history = core.automatic_execution_history
register_registration_conflict_executor()


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
    """Extend the existing E4 runner with exact guarded acquisition finalization."""
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
            }
            or (
                item.get("planKind") == "RECONCILE_ADMISSION"
                and item.get("subjectKind") == "admission"
            )
        )
        and item.get("state") in {"planned", "ready", "retry_wait"}
    ]
    candidates.sort(key=lambda item: int(item["id"]))

    if candidates and str(candidates[0].get("planKind") or "") == "CORRECT_REGISTRATION_CONFLICT":
        return run_registration_conflict_cycle(candidates[0])
    if candidates and str(candidates[0].get("planKind") or "") == "FINALIZE_ACQUISITION":
        return _run_finalization_cycle(candidates[0])
    return core.run_automatic_cycle(limit)
