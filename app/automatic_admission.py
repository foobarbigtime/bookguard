from __future__ import annotations

from typing import Any

from .admission import (
    AdmissionSafetyError, admission_reconcile_preview, reconcile_admission,
)
from .automatic_contracts import AutomaticExecutionBlocked
from .bindery_client import BinderyClient
from .db import ebook_admission_by_id, local_conn
from .observe import _admission_decisions
from .recovery_planner import _build_plan


class _KnownAdmissionReconcileExecutor:
    action_code = "reconcile_known_admission"

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
        if str(plan.get("planKind") or "") != "RECONCILE_ADMISSION":
            raise AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH",
                "reconcile_known_admission is valid only for RECONCILE_ADMISSION.",
            )
        if str(plan.get("subjectKind") or "") != "admission":
            raise AutomaticExecutionBlocked(
                "SUBJECT_KIND_MISMATCH",
                "Live admission reconciliation requires an admission subject.",
            )
        reason_code = str(plan.get("reasonCode") or "")
        if reason_code not in {
            "REGISTRATION_SCAN_PENDING", "REGISTRATION_SCAN_OUTCOME_UNKNOWN"
        }:
            raise AutomaticExecutionBlocked(
                "REASON_CODE_MISMATCH",
                "Live admission reconciliation requires an exact known scan state.",
            )

        admission_id = int(plan["subjectId"])
        admission = ebook_admission_by_id(admission_id)
        if not admission:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The planned admission no longer exists.",
            )

        current_plan = self._current_plan(admission_id)
        if not current_plan:
            raise AutomaticExecutionBlocked(
                "DECISION_NO_LONGER_AUTHORIZED",
                "The current decision policy no longer authorizes admission reconciliation.",
            )

        try:
            preview = admission_reconcile_preview(admission_id, BinderyClient())
        except AdmissionSafetyError as exc:
            raise AutomaticExecutionBlocked(
                "ADMISSION_RECONCILE_PREFLIGHT_FAILED",
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
                    and int(preview.get("admissionId") or 0) == admission_id
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
                "code": "WORKFLOW_STATE_UNCHANGED",
                "ok": (
                    bool(preview.get("scanRequestFailureProven"))
                    if reason_code == "REGISTRATION_SCAN_OUTCOME_UNKNOWN"
                    else bool(preview_checks.get("workflowStateScanRequested"))
                ),
            },
            {
                "code": "UNCERTAIN_SCAN_PROVEN_WITHOUT_RETRY",
                "ok": (
                    reason_code != "REGISTRATION_SCAN_OUTCOME_UNKNOWN"
                    or str(preview.get("registrationState") or "") == "registered"
                ),
            },
            {
                "code": "PUBLISHED_BYTES_UNCHANGED",
                "ok": bool(preview_checks.get("publishedBytesCurrent")),
            },
            {
                "code": "STAGED_BYTES_UNCHANGED",
                "ok": bool(preview_checks.get("stagedBytesCurrent")),
            },
            {
                "code": "RESULT_IDENTITY_UNCHANGED",
                "ok": bool(preview_checks.get("resultIdentityUnchanged")),
            },
            {
                "code": "BINDERY_OWNERSHIP_CONSISTENT",
                "ok": bool(preview_checks.get("binderyOwnershipConsistent")),
            },
            {
                "code": "ADMISSION_ACTIONS_ENABLED",
                "ok": bool(preview_checks.get("actionsEnabled")),
            },
            {
                "code": "ADMISSION_MODE_ENABLED",
                "ok": bool(preview_checks.get("admissionEnabled")),
            },
        ]

        return {
            "ok": all(check["ok"] for check in checks),
            "planSignature": str(plan["signature"]),
            "evidenceRevision": str(plan["evidenceRevision"]),
            "checks": checks,
            "admissionId": admission_id,
            "resultId": int(preview["resultId"]),
            "bookId": int(preview["bookId"]),
            "storedPath": str(preview["storedPath"]),
            "stagedSha256": str(preview["stagedSha256"]),
            "registrationState": str(preview["registrationState"]),
        }

    def reconcile_uncertain(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        admission_id = int(plan["subjectId"])
        admission = ebook_admission_by_id(admission_id)
        if not admission:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The interrupted admission no longer exists.",
            )

        try:
            preview = admission_reconcile_preview(admission_id, BinderyClient())
        except AdmissionSafetyError as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Interrupted admission reconciliation could not be proven safely: {exc}",
            ) from exc

        state = str(preview.get("registrationState") or "")
        if state != "registered":
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "The prior Bindery scan request cannot be proven complete from current "
                "registration state, so Automatic Mode will not replay it.",
            )

        try:
            outcome = reconcile_admission(
                admission_id, BinderyClient(), allow_scan=False
            )
        except AdmissionSafetyError as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Proven registration could not be adopted safely: {exc}",
            ) from exc

        if str(outcome.get("status") or "") != "registered":
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Current Bindery evidence did not reconcile to registered state.",
            )

        return {
            "admissionId": admission_id,
            "bookId": int(outcome["bookId"]),
            "status": "registered",
            "registered": True,
            "scanRequested": False,
            "externalMutationPerformed": False,
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
            # A scan_requested status means a request was already sent. Automatic
            # reconciliation only adopts independently proven registration.
            outcome = reconcile_admission(
                admission_id, BinderyClient(), allow_scan=False
            )
        except AdmissionSafetyError as exc:
            raise RuntimeError(str(exc)) from exc

        return {
            "admissionId": admission_id,
            "bookId": int(outcome["bookId"]),
            "status": str(outcome.get("status") or ""),
            "registered": bool(outcome.get("registered")),
            "scanRequested": bool(outcome.get("scanRequested")),
            "registrationConflict": outcome.get("registrationConflict"),
            "externalMutationPerformed": bool(outcome.get("scanRequested")),
            "libraryBytesChanged": False,
            "stagingRetained": bool(outcome.get("stagingRetained")),
        }
