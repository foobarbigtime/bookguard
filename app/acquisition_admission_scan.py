"""One separately allowlisted Bindery scan for a proven acquisition publication."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import automatic_execution as core
from .acquisition_admission_preflight import _source_identity
from .admission import (
    AdmissionSafetyError, _admission_lock, _book_has_ebook, _result_matches_book,
    admission_readiness, admission_reconcile_preview, reconcile_admission,
)
from .bindery_client import BinderyClient, BinderyClientError
from .config import load_automation_settings, settings
from .db import (
    ebook_acquisition_by_admission_id, ebook_admission_by_id, local_conn,
    result_by_id, utc_now,
)
from .observe import _admission_decisions
from .recovery_planner import _build_plan
from .staging import StagingSafetyError


_ACTION = "request_published_acquisition_scan"
_BOUND = (
    "planSignature", "evidenceRevision", "admissionId", "acquisitionId",
    "resultId", "bookId", "storedPath", "stagedSha256",
    "publicationMethod", "publishedPath", "sourceIdentity",
)


def _publication_receipt(admission_id: int, acquisition_id: int) -> dict[str, Any]:
    with local_conn() as conn:
        rows = conn.execute(
            """SELECT e.* FROM automatic_executions e
               JOIN recovery_plans p ON p.signature=e.plan_signature
               WHERE p.plan_kind='PREPARE_ACQUISITION_ADMISSION'
                 AND p.subject_kind='acquisition' AND p.subject_id=?
                 AND e.action_code='admit_verified_acquisition'
                 AND e.step_index=1 AND e.state='succeeded'""",
            (str(acquisition_id),),
        ).fetchall()
    receipts = [core._decode(row) for row in rows]
    if len(receipts) != 1:
        raise AdmissionSafetyError("Exactly one completed acquisition publication is required.")
    receipt = receipts[0]
    result = receipt["externalResult"]
    boundary = receipt["boundary"]
    if not all((
        result.get("admissionId") == admission_id,
        result.get("acquisitionId") == acquisition_id,
        result.get("status") == "published",
        result.get("scanRequested") is False,
        result.get("stagedSha256") == boundary.get("stagedSha256"),
        bool(result.get("publicationMethod")),
        boundary.get("planSignature") == receipt["planSignature"],
        boundary.get("evidenceRevision") == receipt["evidenceRevision"],
        boundary.get("acquisitionId") == acquisition_id,
        len(boundary.get("sourceIdentity") or []) == 5,
    )):
        raise AdmissionSafetyError("The acquisition publication receipt is incomplete.")
    return receipt


def _prior_scan(admission_id: int, *, except_signature: str = "") -> bool:
    """Any earlier attempted scan for this admission forbids a new POST."""
    with local_conn() as conn:
        rows = conn.execute(
            """SELECT plan_signature, boundary_json FROM automatic_executions
               WHERE action_code=? AND attempt_count>0""", (_ACTION,),
        ).fetchall()
    for row in rows:
        if except_signature and row["plan_signature"] == except_signature:
            continue
        try:
            boundary = json.loads(row["boundary_json"] or "{}")
        except (TypeError, ValueError):
            raise AdmissionSafetyError("An earlier scan journal is unreadable.") from None
        if not isinstance(boundary, dict):
            raise AdmissionSafetyError("An earlier scan journal is malformed.")
        if boundary.get("admissionId") == admission_id:
            return True
    return False


def _mark_scan_requested(plan: dict[str, Any], boundary: dict[str, Any]) -> None:
    with local_conn() as conn:
        changed = conn.execute(
            """UPDATE ebook_admissions SET status='scan_requested', error=NULL,
                      updated_at=?
               WHERE id=? AND status='published' AND result_id=? AND book_id=?
                 AND stored_path=? AND staged_sha256=? AND publication_method=?""",
            (
                utc_now(), int(plan["subjectId"]), boundary["resultId"],
                boundary["bookId"], boundary["storedPath"],
                boundary["stagedSha256"], boundary["publicationMethod"],
            ),
        ).rowcount
        if changed != 1:
            raise AdmissionSafetyError("Admission changed after the scan request.")
        conn.commit()


class _PublishedAcquisitionScanExecutor:
    action_code = _ACTION

    def _observe(self, plan: dict[str, Any], statuses: set[str]) -> dict[str, Any]:
        admission_id = int(plan["subjectId"])
        admission = ebook_admission_by_id(admission_id)
        acquisition = ebook_acquisition_by_admission_id(admission_id)
        result = result_by_id(int(plan["resultId"]))
        if not admission or not acquisition or not result:
            raise AdmissionSafetyError("Linked admission, acquisition, or result is missing.")
        receipt = _publication_receipt(admission_id, int(acquisition["id"]))
        publication = receipt["externalResult"]
        original = receipt["boundary"]
        verification = admission.get("verification") or {}
        if not isinstance(verification, dict):
            verification = {}

        client = BinderyClient()
        preview = admission_reconcile_preview(
            admission_id, client, allow_published_without_scan=True,
        )
        readiness = admission_readiness(client)
        book = client.get_book(int(plan["bookId"]))
        source_identity = list(_source_identity(preview["stagedPath"]))
        state = preview["registrationState"]
        checks = preview["checks"]
        valid = [
            {"code": "SUBJECT_IDENTITY_UNCHANGED", "ok": all((
                admission["status"] in statuses,
                admission["result_id"] == plan["resultId"] == acquisition["result_id"],
                admission["book_id"] == plan["bookId"] == acquisition["book_id"],
                admission["stored_path"] == plan["path"] == result["stored_path"],
                admission["local_path"] == result["local_path"],
                admission["staged_relative_path"] == acquisition["staged_relative_path"],
                admission["staged_sha256"] == acquisition["staged_sha256"],
                acquisition["status"] == "admitted",
                acquisition["admission_id"] == admission_id,
                result["format"] == "ebook",
            ))},
            {"code": "VERIFIED_PUBLICATION_UNCHANGED", "ok": all((
                verification.get("safeToAdmit") is True,
                verification.get("sha256") == admission["staged_sha256"],
                admission["staged_sha256"] == publication["stagedSha256"],
                admission["publication_method"] == publication["publicationMethod"],
                preview["publishedPath"] == original["destination"],
                preview["storedPath"] == original["binderyPath"],
                preview["stagedRelativePath"] == original["stagedRelativePath"],
                checks.get("publishedBytesCurrent") is True,
                checks.get("stagedBytesCurrent") is True,
                source_identity == original["sourceIdentity"],
            ))},
            {"code": "RESULT_IDENTITY_UNCHANGED", "ok": bool(
                checks.get("resultIdentityUnchanged")
                and _result_matches_book(result, book)
            )},
            {"code": "BINDERY_OWNERSHIP_CONSISTENT", "ok": bool(
                checks.get("binderyOwnershipConsistent")
                and state in {"scan_required", "registered"}
                and (state != "scan_required" or not _book_has_ebook(book))
            )},
            {"code": "ADMISSION_READINESS", "ok": readiness["ready"]},
        ]
        return {
            "ok": all(item["ok"] for item in valid), "checks": valid,
            "planSignature": plan["signature"],
            "evidenceRevision": plan["evidenceRevision"],
            "admissionId": admission_id, "acquisitionId": int(acquisition["id"]),
            "resultId": int(plan["resultId"]), "bookId": int(plan["bookId"]),
            "status": admission["status"], "registrationState": state,
            "storedPath": admission["stored_path"],
            "stagedSha256": admission["staged_sha256"],
            "publicationMethod": admission["publication_method"],
            "publishedPath": preview["publishedPath"],
            "sourceIdentity": source_identity,
        }

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if (
            plan.get("planKind"), plan.get("subjectKind"), plan.get("reasonCode")
        ) != (
            "REQUEST_PUBLISHED_ACQUISITION_SCAN", "admission",
            "PUBLISHED_ACQUISITION_ADMISSION_SCAN_READY",
        ) or step.get("code") not in {
            "reobserve_published_acquisition_admission", _ACTION,
        }:
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "Only a proven published acquisition may request a scan."
            )
        if _prior_scan(int(plan["subjectId"])):
            raise core.AutomaticExecutionBlocked(
                "SCAN_PREVIOUSLY_ATTEMPTED", "A prior scan journal forbids another request."
            )
        with local_conn() as conn:
            decision = next((
                item for item in _admission_decisions(conn, 2000)
                if item.get("subjectId") == plan["subjectId"]
            ), None)
            current = _build_plan(conn, decision) if decision else None
        if not current or (
            current["signature"], current["evidenceRevision"]
        ) != (plan["signature"], plan["evidenceRevision"]):
            raise core.AutomaticExecutionBlocked(
                "PLAN_CHANGED", "The published admission plan changed."
            )
        try:
            return self._observe(plan, {"published"})
        except (
            AdmissionSafetyError, BinderyClientError, OSError, sqlite3.Error,
            StagingSafetyError, ValueError, TypeError,
        ) as exc:
            raise core.AutomaticExecutionBlocked(
                "SCAN_PREFLIGHT_FAILED", str(exc)
            ) from exc

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        if not _admission_lock.acquire(blocking=False):
            raise AdmissionSafetyError("Another admission operation is running.")
        try:
            # The current attempt must be durably running before any scan POST.
            with local_conn() as conn:
                attempt = conn.execute(
                    """SELECT state, attempt_count FROM automatic_executions
                       WHERE plan_signature=? AND action_code=? AND step_index=1""",
                    (plan["signature"], _ACTION),
                ).fetchone()
            if not attempt or attempt["state"] != "running" or attempt["attempt_count"] != 1:
                raise AdmissionSafetyError("The exact first scan attempt journal is missing.")
            if _prior_scan(int(plan["subjectId"]), except_signature=plan["signature"]):
                raise AdmissionSafetyError("Another scan attempt exists for this admission.")
            fresh = self._observe(plan, {"published"})
            core._require_fresh_boundary(plan, fresh)
            if any(fresh.get(key) != boundary.get(key) for key in _BOUND) or (
                fresh["status"], fresh["registrationState"]
            ) != ("published", boundary["registrationState"]):
                raise AdmissionSafetyError("The scan boundary changed.")
            if fresh["registrationState"] == "registered":
                outcome = reconcile_admission(int(plan["subjectId"]), BinderyClient(), allow_scan=False)
                return {
                    "admissionId": int(plan["subjectId"]), "status": outcome["status"],
                    "scanRequested": False, "externalMutationPerformed": False,
                }
            configured = load_automation_settings()
            if (
                configured.automation_mode != "automatic"
                or _ACTION not in configured.automatic_action_allowlist
                or not configured.automatic_reacquisition
                or not configured.admission_enabled
                or not settings.allow_actions
            ):
                raise AdmissionSafetyError("The scan action was disabled before its request.")
            client = BinderyClient()
            try:
                client.scan_library()
            except BinderyClientError as exc:
                detail = str(exc).casefold()
                if "http 409" not in detail or not any(
                    marker in detail for marker in ("already running", "scan in progress")
                ):
                    raise
            _mark_scan_requested(plan, fresh)
            return {
                "admissionId": int(plan["subjectId"]), "status": "scan_requested",
                "scanRequested": True, "externalMutationPerformed": True,
                "libraryBytesChanged": False, "stagingRetained": True,
            }
        finally:
            _admission_lock.release()

    def reconcile_uncertain(
        self, plan: dict[str, Any], step: dict[str, Any], existing: dict[str, Any]
    ) -> dict[str, Any]:
        """Adopt a proven status or registration; never send another scan."""
        if not _admission_lock.acquire(blocking=False):
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "Another admission operation is running."
            )
        try:
            prior = existing.get("boundary") or {}
            current = self._observe(
                plan, {"published", "scan_requested", "scan_request_failed", "registered"},
            )
            core._require_fresh_boundary(plan, current)
            if any(prior.get(key) != current.get(key) for key in _BOUND):
                raise AdmissionSafetyError("Interrupted scan boundary changed.")
            if current["registrationState"] == "registered":
                outcome = reconcile_admission(int(plan["subjectId"]), BinderyClient(), allow_scan=False)
                if outcome["status"] != "registered":
                    raise AdmissionSafetyError("Exact Bindery registration was not adopted.")
                status = "registered"
            elif current["status"] == "scan_requested" and current["registrationState"] == "scan_required":
                status = "scan_requested"
            else:
                raise AdmissionSafetyError("Scan outcome is uncertain; a second request is forbidden.")
            return {
                "admissionId": int(plan["subjectId"]), "status": status,
                "scanRequested": False, "externalMutationPerformed": False,
                "libraryBytesChanged": False, "stagingRetained": True,
                "reconciledAfterRestart": True,
            }
        except (
            AdmissionSafetyError, BinderyClientError, OSError, sqlite3.Error,
            StagingSafetyError, ValueError, TypeError,
        ) as exc:
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", str(exc)
            ) from exc
        finally:
            _admission_lock.release()


EXECUTOR = _PublishedAcquisitionScanExecutor()


def register_executor() -> None:
    core.register_automatic_executor(_ACTION, EXECUTOR)


def run_published_acquisition_scan_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = int(plan["id"])
    steps = list(plan.get("steps") or [])
    index = int(plan.get("currentStep") or 0)
    if index == 0 and steps[0].get("code") == "reobserve_published_acquisition_admission":
        try:
            boundary = EXECUTOR.revalidate(plan, steps[0])
            core._require_fresh_boundary(plan, boundary)
        except core.AutomaticExecutionBlocked as exc:
            blocked = core.block_recovery_plan(plan_id, str(exc))
            return {"ok": False, "state": "blocked", "plan": blocked,
                    "reasonCode": exc.reason_code, "externalMutationAttempted": False}
        plan = core.record_recovery_step_success(plan_id, 0)
        steps = list(plan.get("steps") or [])
        index = int(plan.get("currentStep") or 0)
    if index != 1 or len(steps) != 2 or steps[index].get("code") != _ACTION:
        return {"ok": True, "state": "paused", "plan": plan,
                "externalMutationAttempted": False}
    try:
        result = core.attempt_automatic_step(plan_id)
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(plan_id, str(exc))
        return {"ok": False, "state": "blocked", "plan": blocked,
                "reasonCode": exc.reason_code,
                "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED"}
    external = dict((result.get("execution") or {}).get("externalResult") or {})
    return {
        **result,
        "state": "reconciled" if result.get("reconciled") else
                 "replayed" if result.get("replayed") else "executed",
        "externalMutationAttempted": bool(external.get("externalMutationPerformed", False)),
    }
