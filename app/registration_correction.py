from __future__ import annotations

import os
import sqlite3
from typing import Any

from . import automatic_execution as core
from .admission import (
    AdmissionSafetyError,
    _bindery_setting,
    _book_has_ebook,
    _book_has_exact_ebook,
    _complete_queue_items,
    _exact_ebook_associations,
    _result_matches_book,
    _verified_published_destination,
    _verified_staged_source,
    correct_registration_conflict,
)
from .bindery_client import BinderyClient, BinderyClientError
from .config import load_automation_settings, settings
from .db import (
    ebook_acquisition_by_admission_id,
    ebook_admission_by_id,
    local_conn,
    result_by_id,
    update_ebook_admission,
)
from .observe import _admission_decisions
from .recovery_planner import _build_plan


AutomaticExecutionBlocked = core.AutomaticExecutionBlocked

_HISTORICAL_QUEUE_STATUSES = {
    "cancelled",
    "failed",
    "importblocked",
    "imported",
    "removed",
}


def registration_conflict_preview(
    admission_id: int,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Read-only proof for exact-path registration conflict correction.

    The initial correction boundary is intentionally strict: one durable
    registration_conflict admission, one matching acquisition/queue record,
    one foreign exact-path ebook association, unchanged staged/published bytes,
    unchanged intended book identity, external import mode, disabled auto-grab,
    and an exact no-move reassignment preview.

    Crash recovery is stricter still. A running correction is never replayed.
    It may only be adopted when current API/database evidence independently
    proves the exact target owner and all byte/settings invariants are restored.
    """
    configured = load_automation_settings()
    admission = ebook_admission_by_id(int(admission_id))
    if not admission:
        raise AdmissionSafetyError("Admission record not found.")

    status = str(admission.get("status") or "").casefold()
    expected_book_id = int(admission.get("book_id") or 0)
    result_id = int(admission.get("result_id") or 0)
    stored_path = str(admission.get("stored_path") or "")
    staged_sha256 = str(admission.get("staged_sha256") or "")

    result = result_by_id(result_id)
    result_matches = bool(
        result
        and int(result.get("book_id") or 0) == expected_book_id
        and os.path.normpath(str(result.get("stored_path") or ""))
        == os.path.normpath(stored_path)
        and str(result.get("local_path") or "")
        == str(admission.get("local_path") or "")
    )

    published_path = _verified_published_destination(admission, configured)
    staged_path = _verified_staged_source(admission)

    try:
        acquisition = ebook_acquisition_by_admission_id(int(admission_id))
    except sqlite3.Error as exc:
        raise AdmissionSafetyError(
            f"The linked acquisition could not be proven unique: {exc}"
        ) from exc
    acquisition_matches = bool(
        acquisition
        and str(acquisition.get("status") or "").casefold() == "admitted"
        and int(acquisition.get("book_id") or 0) == expected_book_id
        and int(acquisition.get("result_id") or 0) == result_id
        and int(acquisition.get("admission_id") or 0) == int(admission_id)
        and acquisition.get("queue_id") is not None
    )
    queue_id = acquisition.get("queue_id") if acquisition else None

    client = client or BinderyClient()
    try:
        auto_grab = _bindery_setting(client, "autoGrab.enabled").casefold()
        import_mode = (_bindery_setting(client, "import.mode").casefold() or "auto")
        target_book = client.get_book(expected_book_id)
        exact_associations = _exact_ebook_associations(stored_path)
        queue_items = _complete_queue_items(client)
    except (BinderyClientError, sqlite3.Error) as exc:
        raise AdmissionSafetyError(
            f"Bindery correction boundary could not be read safely: {exc}"
        ) from exc

    target_identity_matches = bool(result and _result_matches_book(result, target_book))
    target_registered = _book_has_exact_ebook(target_book, stored_path)
    exact_target = [
        item
        for item in exact_associations
        if int(item.get("book_id") or 0) == expected_book_id
    ]
    exact_foreign = [
        item
        for item in exact_associations
        if int(item.get("book_id") or 0) != expected_book_id
    ]

    if target_registered and len(exact_associations) == 1 and len(exact_target) == 1:
        registration_state = "corrected"
    elif not target_registered and len(exact_associations) == 1 and len(exact_foreign) == 1:
        registration_state = "conflict"
    else:
        registration_state = "inconsistent"

    active_queue = [
        item
        for item in queue_items
        if str(item.get("status") or "").casefold()
        not in _HISTORICAL_QUEUE_STATUSES
    ]
    queue_matches = [
        item
        for item in queue_items
        if queue_id is not None and str(item.get("id") or "") == str(queue_id)
    ]
    queue_item = queue_matches[0] if len(queue_matches) == 1 else None
    unrelated_active = [
        item
        for item in active_queue
        if queue_id is None or str(item.get("id") or "") != str(queue_id)
    ]
    queue_identity_matches = bool(
        queue_item
        and int(queue_item.get("bookId") or 0) == expected_book_id
        and str(queue_item.get("status") or "").casefold() == "importexternal"
    )

    preview_exact_no_move = False
    if registration_state == "conflict":
        try:
            preview = client.preview_manual_reassignment(
                stored_path,
                expected_book_id,
                file_format="ebook",
            )
        except BinderyClientError as exc:
            raise AdmissionSafetyError(str(exc)) from exc
        preview_exact_no_move = bool(
            isinstance(preview, dict)
            and os.path.normpath(str(preview.get("source") or ""))
            == os.path.normpath(stored_path)
            and os.path.normpath(str(preview.get("destination") or ""))
            == os.path.normpath(stored_path)
            and str(preview.get("format") or "").casefold() == "ebook"
            and str(preview.get("status") or "").casefold() == "noop"
        )

    common_checks = {
        "actionsEnabled": bool(settings.allow_actions),
        "automaticReacquisitionEnabled": bool(configured.automatic_reacquisition),
        "admissionEnabled": bool(configured.admission_enabled),
        "verifiedSnapshotPresent": bool(staged_sha256),
        "resultIdentityUnchanged": result_matches,
        "acquisitionIdentityUnchanged": acquisition_matches,
        "targetBookIdentityUnchanged": target_identity_matches,
        "publishedBytesCurrent": published_path.is_file(),
        "stagedBytesCurrent": staged_path.is_file(),
        "autoGrabDisabled": auto_grab == "false",
        "externalImportMode": import_mode == "external",
    }
    correction_checks = {
        **common_checks,
        "workflowStateConflict": status == "registration_conflict",
        "exactForeignOwner": registration_state == "conflict",
        "targetBookHasNoOtherEbook": not _book_has_ebook(target_book),
        "queueIdPresent": queue_id is not None,
        "queueIdentityUnambiguous": len(queue_matches) == 1,
        "queueIdentityCurrent": queue_identity_matches,
        "noUnrelatedActiveQueue": not unrelated_active,
        "reassignmentPreviewExactNoMove": preview_exact_no_move,
    }
    adoption_checks = {
        **common_checks,
        "workflowStateRecoverable": status in {"registration_correcting", "registered"},
        "exactTargetOwner": registration_state == "corrected",
    }
    correction_blockers = [
        name for name, passed in correction_checks.items() if not passed
    ]
    adoption_blockers = [
        name for name, passed in adoption_checks.items() if not passed
    ]

    return {
        "safe": not correction_blockers,
        "adoptionSafe": not adoption_blockers,
        "checks": correction_checks,
        "adoptionChecks": adoption_checks,
        "blockers": correction_blockers,
        "adoptionBlockers": adoption_blockers,
        "admissionId": int(admission["id"]),
        "resultId": result_id,
        "bookId": expected_book_id,
        "status": status,
        "registrationState": registration_state,
        "storedPath": stored_path,
        "localPath": str(admission.get("local_path") or ""),
        "stagedRelativePath": str(admission.get("staged_relative_path") or ""),
        "stagedSha256": staged_sha256,
        "publishedPath": str(published_path),
        "stagedPath": str(staged_path),
        "queueId": queue_id,
        "queuePresent": queue_item is not None,
        "exactAssociations": exact_associations,
        "importMode": import_mode,
        "autoGrabEnabled": auto_grab,
    }


class RegistrationConflictExecutor:
    action_code = "correct_exact_registration_owner"

    def _current_plan(self, admission_id: int) -> dict[str, Any] | None:
        with local_conn() as conn:
            decisions = _admission_decisions(conn, 500)
            current_decision = next(
                (
                    item
                    for item in decisions
                    if str(item.get("subjectKind") or "") == "admission"
                    and str(item.get("subjectId") or "") == str(admission_id)
                ),
                None,
            )
            return _build_plan(conn, current_decision) if current_decision else None

    def revalidate(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
    ) -> dict[str, Any]:
        if str(plan.get("planKind") or "") != "CORRECT_REGISTRATION_CONFLICT":
            raise AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH",
                "correct_exact_registration_owner is valid only for CORRECT_REGISTRATION_CONFLICT.",
            )
        if str(plan.get("subjectKind") or "") != "admission":
            raise AutomaticExecutionBlocked(
                "SUBJECT_KIND_MISMATCH",
                "Registration conflict correction requires an admission subject.",
            )
        if str(plan.get("reasonCode") or "") != "REGISTRATION_CONFLICT":
            raise AutomaticExecutionBlocked(
                "REASON_CODE_MISMATCH",
                "Live registration correction is limited to REGISTRATION_CONFLICT.",
            )

        admission_id = int(plan["subjectId"])
        if not ebook_admission_by_id(admission_id):
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The planned admission no longer exists.",
            )

        current_plan = self._current_plan(admission_id)
        if not current_plan:
            raise AutomaticExecutionBlocked(
                "DECISION_NO_LONGER_AUTHORIZED",
                "The current policy no longer authorizes exact registration correction.",
            )

        try:
            preview = registration_conflict_preview(admission_id, BinderyClient())
        except (AdmissionSafetyError, BinderyClientError) as exc:
            raise AutomaticExecutionBlocked(
                "REGISTRATION_CORRECTION_PREFLIGHT_FAILED",
                str(exc),
            ) from exc

        preview_checks = dict(preview.get("checks") or {})
        checks = [
            {
                "code": "SUBJECT_IDENTITY_UNCHANGED",
                "ok": (
                    str(current_plan.get("subjectId") or "")
                    == str(plan.get("subjectId") or "")
                    and current_plan.get("resultId") == plan.get("resultId")
                    and current_plan.get("bookId") == plan.get("bookId")
                    and int(preview.get("admissionId") or 0) == admission_id
                ),
            },
            {
                "code": "PATH_UNCHANGED",
                "ok": str(current_plan.get("path") or "")
                == str(plan.get("path") or ""),
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
        ]
        checks.extend(
            {"code": name, "ok": bool(value)}
            for name, value in preview_checks.items()
        )

        return {
            "ok": bool(preview.get("safe")) and all(check["ok"] for check in checks),
            "planSignature": str(plan["signature"]),
            "evidenceRevision": str(plan["evidenceRevision"]),
            "checks": checks,
            "admissionId": admission_id,
            "resultId": int(preview["resultId"]),
            "bookId": int(preview["bookId"]),
            "storedPath": str(preview["storedPath"]),
            "stagedSha256": str(preview["stagedSha256"]),
            "queueId": preview.get("queueId"),
            "registrationState": str(preview["registrationState"]),
        }

    def verify_corrected(self, admission_id: int) -> dict[str, Any]:
        try:
            preview = registration_conflict_preview(admission_id, BinderyClient())
        except (AdmissionSafetyError, BinderyClientError) as exc:
            raise AutomaticExecutionBlocked(
                "REGISTRATION_CORRECTION_VERIFY_FAILED",
                str(exc),
            ) from exc
        if preview.get("adoptionSafe") is not True:
            raise AutomaticExecutionBlocked(
                "REGISTRATION_CORRECTION_VERIFY_FAILED",
                "Corrected registration could not be independently proven: "
                + ", ".join(preview.get("adoptionBlockers") or []),
            )
        return preview

    def reconcile_uncertain(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        admission_id = int(plan["subjectId"])
        try:
            preview = registration_conflict_preview(admission_id, BinderyClient())
        except (AdmissionSafetyError, BinderyClientError) as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Interrupted registration correction could not be proven safely: {exc}",
            ) from exc

        if preview.get("adoptionSafe") is not True:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted registration correction will not be replayed. Current "
                "API/database evidence does not independently prove the intended exact "
                "owner with restored settings and unchanged bytes.",
            )

        update_ebook_admission(admission_id, "registered")
        return {
            "admissionId": admission_id,
            "bookId": int(preview["bookId"]),
            "status": "registered",
            "registrationState": "corrected",
            "externalMutationPerformed": False,
            "libraryBytesChanged": False,
            "stagingRetained": True,
            "reconciledAfterRestart": True,
        }

    def execute(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any]:
        admission_id = int(plan["subjectId"])
        try:
            outcome = correct_registration_conflict(admission_id, BinderyClient())
        except AdmissionSafetyError as exc:
            raise RuntimeError(str(exc)) from exc
        if str(outcome.get("status") or "") != "registered":
            raise RuntimeError("Guarded registration correction did not reach registered state.")
        already_corrected = bool(outcome.get("alreadyCorrected"))
        return {
            "admissionId": admission_id,
            "bookId": int(outcome["bookId"]),
            "status": "registered",
            "alreadyCorrected": already_corrected,
            "queueRecordRemoved": bool(outcome.get("queueRecordRemoved")),
            "externalMutationPerformed": not already_corrected,
            "removedFromDownloadClient": bool(outcome.get("removedFromDownloadClient", False)),
            "downloadedDataDeleted": bool(outcome.get("downloadedDataDeleted", False)),
            "libraryBytesChanged": bool(outcome.get("libraryBytesChanged", False)),
            "stagingRetained": bool(outcome.get("stagedFileRetained", True)),
        }


REGISTRATION_CONFLICT_EXECUTOR = RegistrationConflictExecutor()


def register_executor() -> None:
    core.register_automatic_executor(
        "correct_exact_registration_owner",
        REGISTRATION_CONFLICT_EXECUTOR,
    )


def run_registration_conflict_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    """Advance one E4 exact-path correction with at most one external mutation."""
    plan_id = int(plan["id"])
    refreshed = core.recovery_plan_by_id(plan_id) or plan
    steps = list(refreshed.get("steps") or [])
    index = int(refreshed.get("currentStep") or 0)
    code = (
        str((steps[index] or {}).get("code") or "")
        if 0 <= index < len(steps)
        else ""
    )

    if code == "revalidate_registration_conflict":
        try:
            boundary = REGISTRATION_CONFLICT_EXECUTOR.revalidate(refreshed, steps[index])
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

    result: dict[str, Any] | None = None
    if code == "correct_exact_registration_owner":
        try:
            result = core.attempt_automatic_step(plan_id)
        except AutomaticExecutionBlocked as exc:
            if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
                raise
            blocked = core.block_recovery_plan(
                plan_id,
                "Exact registration correction stopped safely: " + str(exc),
            )
            return {
                "ok": False,
                "state": "blocked",
                "plan": blocked,
                "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED",
                "reasonCode": exc.reason_code,
                "message": str(exc),
            }

    refreshed = core.recovery_plan_by_id(plan_id) or refreshed
    steps = list(refreshed.get("steps") or [])
    index = int(refreshed.get("currentStep") or 0)
    code = (
        str((steps[index] or {}).get("code") or "")
        if 0 <= index < len(steps)
        else ""
    )
    if code == "verify_registration_correction":
        try:
            REGISTRATION_CONFLICT_EXECUTOR.verify_corrected(int(refreshed["subjectId"]))
        except AutomaticExecutionBlocked as exc:
            blocked = core.block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False,
                "state": "blocked",
                "plan": blocked,
                "externalMutationAttempted": bool(result and not result.get("replayed")),
                "reasonCode": exc.reason_code,
                "message": str(exc),
            }
        refreshed = core.record_recovery_step_success(plan_id, index)

    if result is None:
        return {
            "ok": True,
            "state": "verified" if str(refreshed.get("state") or "") == "completed" else "paused",
            "plan": refreshed,
            "externalMutationAttempted": False,
            "message": "Registration correction verification completed without replaying a mutation.",
        }

    replayed = bool(result.get("replayed"))
    reconciled = bool(result.get("reconciled"))
    external_result = dict((result.get("execution") or {}).get("externalResult") or {})
    return {
        **result,
        "plan": refreshed,
        "state": "reconciled" if reconciled else "replayed" if replayed else "executed",
        "externalMutationAttempted": bool(
            external_result.get("externalMutationPerformed", not replayed)
        ),
    }
