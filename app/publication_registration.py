"""One guarded Bindery scan after a proven and published admission."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from . import automatic_execution as core
from .admission import (
    AdmissionSafetyError, _admission_lock, _book_has_ebook,
    _result_matches_book, admission_readiness, admission_reconcile_preview,
    reconcile_admission,
)
from .bindery_client import BinderyClient, BinderyClientError
from .db import ebook_admission_by_id, local_conn, result_by_id, utc_now
from .publication_recovery import _bound, _proven_receipt
from .staging import StagingSafetyError


_ACTION = "scan_and_reconcile_registration"


def _published_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    receipt = core._existing(str(plan["signature"]), "retry_guarded_publication", 4)
    result = (receipt or {}).get("externalResult") or {}
    boundary = (receipt or {}).get("boundary") or {}
    if not (
        receipt and receipt.get("state") == "succeeded"
        and result.get("admissionId") == int(plan["subjectId"])
        and result.get("bookId") == int(plan["bookId"])
        and result.get("status") == "published"
        and result.get("stagedSha256") == boundary.get("stagedSha256")
        and result.get("publicationMethod")
        and result.get("binderyScanRequested") is False
        and boundary.get("planSignature") == plan.get("signature")
        and boundary.get("evidenceRevision") == plan.get("evidenceRevision")
    ):
        raise core.AutomaticExecutionBlocked(
            "PUBLICATION_NOT_PROVEN", "The exact admission has no completed publication journal."
        )
    return receipt


def _scan_requested(plan: dict[str, Any], method: str, sha256: str) -> None:
    with local_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE ebook_admissions
            SET status='scan_requested', error=NULL, updated_at=?
            WHERE id=? AND status='published' AND publication_method=?
              AND staged_sha256=? AND result_id=? AND book_id=? AND stored_path=?
            """,
            (
                utc_now(), int(plan["subjectId"]), method, sha256,
                int(plan["resultId"]), int(plan["bookId"]), str(plan["path"]),
            ),
        )
        if cursor.rowcount != 1:
            raise AdmissionSafetyError("Admission changed after the Bindery scan request.")
        conn.commit()


class _KnownPublicationRegistrationExecutor:
    action_code = _ACTION

    def _observe(
        self, plan: dict[str, Any], allowed_statuses: set[str]
    ) -> dict[str, Any]:
        proof = _proven_receipt(plan)
        publication = _published_receipt(plan)
        admission = ebook_admission_by_id(int(plan["subjectId"]))
        result = result_by_id(int(plan["resultId"]))
        if not admission or not result:
            raise core.AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED", "The published admission or scan result is missing."
            )
        if str(admission.get("status") or "") not in allowed_statuses:
            raise core.AutomaticExecutionBlocked(
                "WORKFLOW_STATE_CHANGED", "The admission is not in an allowed registration state."
            )

        try:
            client = BinderyClient()
            readiness = admission_readiness(client)
            preview = admission_reconcile_preview(int(plan["subjectId"]), client)
            book = client.get_book(int(plan["bookId"]))
            parent = Path(str(preview["publishedPath"])).parent.stat()
            title_matches = _result_matches_book(result, book)
        except (
            AdmissionSafetyError, BinderyClientError, OSError, sqlite3.Error,
            StagingSafetyError, ValueError,
        ) as exc:
            raise core.AutomaticExecutionBlocked(
                "REGISTRATION_PREFLIGHT_FAILED", str(exc)
            ) from exc

        state = str(preview.get("registrationState") or "")
        checks = dict(preview.get("checks") or {})
        pub_result = publication["externalResult"]
        verification = admission.get("verification") or {}
        if not isinstance(verification, dict):
            verification = {}
        evidence = [
            {"code": "SUBJECT_IDENTITY_UNCHANGED", "ok": all((
                str(admission.get("id") or "") == str(plan.get("subjectId") or ""),
                admission.get("result_id") == plan.get("resultId"),
                admission.get("book_id") == plan.get("bookId"),
                result.get("id") == plan.get("resultId"),
                result.get("book_id") == plan.get("bookId"),
                result.get("stored_path") == plan.get("path"),
                result.get("local_path") == admission.get("local_path"),
                admission.get("stored_path") == plan.get("path"),
            ))},
            {"code": "VERIFIED_PUBLICATION_UNCHANGED", "ok": all((
                str(admission.get("staged_sha256") or "") == proof["stagedSha256"],
                str(preview.get("stagedSha256") or "") == proof["stagedSha256"],
                admission.get("publication_method") == pub_result["publicationMethod"],
                _bound(publication["boundary"]) == _bound(proof),
                checks.get("publishedBytesCurrent") is True,
                checks.get("stagedBytesCurrent") is True,
                verification.get("safeToAdmit") is True,
                verification.get("sha256") == proof["stagedSha256"],
            ))},
            {"code": "DESTINATION_FILESYSTEM_UNCHANGED", "ok": (
                (str(Path(preview["publishedPath"]).parent), parent.st_dev, parent.st_ino)
                == (proof["destinationParent"], proof["parentDevice"], proof["parentInode"])
            )},
            {"code": "ADMISSION_READINESS", "ok": readiness["ready"]},
            {"code": "RESULT_IDENTITY_UNCHANGED", "ok": bool(
                checks.get("resultIdentityUnchanged") and title_matches
            )},
            {"code": "BINDERY_OWNERSHIP_CONSISTENT", "ok": bool(
                checks.get("binderyOwnershipConsistent")
                and state in {"scan_required", "registered"}
                and (state != "scan_required" or not _book_has_ebook(book))
            )},
        ]
        return {
            "ok": all(item["ok"] for item in evidence), "checks": evidence,
            "planSignature": plan["signature"],
            "evidenceRevision": plan["evidenceRevision"],
            "admissionId": int(plan["subjectId"]),
            "status": str(admission["status"]), "registrationState": state,
            "stagedSha256": proof["stagedSha256"],
            "storedPath": str(plan["path"]),
            "publicationMethod": str(pub_result["publicationMethod"]),
        }

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if step.get("code") != _ACTION or (
            plan.get("planKind"), plan.get("subjectKind"), plan.get("reasonCode")
        ) != (
            "RECOVER_ADMISSION_PUBLICATION", "admission",
            "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED",
        ):
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "Only the exact published admission may request a scan."
            )
        return self._observe(plan, {"published"})

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        if not _admission_lock.acquire(blocking=False):
            raise AdmissionSafetyError("Another admission operation is already running.")
        try:
            fresh = self.revalidate(plan, step)
            core._require_fresh_boundary(plan, fresh)
            if (
                fresh["status"], fresh["registrationState"], fresh["stagedSha256"],
                fresh["publicationMethod"], fresh["storedPath"],
            ) != (
                boundary["status"], boundary["registrationState"],
                boundary["stagedSha256"], boundary["publicationMethod"],
                boundary["storedPath"],
            ):
                raise AdmissionSafetyError("Registration boundary changed before the scan request.")

            admission_id = int(plan["subjectId"])
            client = BinderyClient()
            if fresh["registrationState"] == "registered":
                outcome = reconcile_admission(admission_id, client, allow_scan=False)
                return {
                    "admissionId": admission_id, "bookId": int(plan["bookId"]),
                    "status": str(outcome["status"]), "registered": True,
                    "scanRequested": False, "externalMutationPerformed": False,
                    "libraryBytesChanged": False, "stagingRetained": True,
                }

            try:
                client.scan_library()
            except BinderyClientError as exc:
                detail = str(exc).casefold()
                if "http 409" not in detail or not any(
                    marker in detail for marker in ("already running", "scan in progress")
                ):
                    raise
            _scan_requested(plan, fresh["publicationMethod"], fresh["stagedSha256"])
            return {
                "admissionId": admission_id, "bookId": int(plan["bookId"]),
                "status": "scan_requested", "registered": False,
                "scanRequested": True, "externalMutationPerformed": True,
                "libraryBytesChanged": False, "stagingRetained": True,
            }
        finally:
            _admission_lock.release()

    def reconcile_uncertain(
        self, plan: dict[str, Any], step: dict[str, Any], existing: dict[str, Any]
    ) -> dict[str, Any]:
        """An interrupted scan may be adopted; never send another scan request."""
        if not _admission_lock.acquire(blocking=False):
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "Another admission operation is running."
            )
        try:
            prior = existing.get("boundary") or {}
            current = self._observe(
                plan, {"published", "scan_requested", "scan_request_failed", "registered"}
            )
            core._require_fresh_boundary(plan, current)
            if any(prior.get(key) != current.get(key) for key in (
                "planSignature", "evidenceRevision", "stagedSha256",
                "storedPath", "publicationMethod",
            )):
                raise AdmissionSafetyError("Interrupted scan boundary changed.")
            state = current["registrationState"]
            status = current["status"]
            if state == "registered":
                outcome = reconcile_admission(
                    int(plan["subjectId"]), BinderyClient(), allow_scan=False
                )
                if outcome["status"] != "registered":
                    raise AdmissionSafetyError("Current Bindery owner was not adopted.")
                status = "registered"
            elif status != "scan_requested" or state != "scan_required":
                raise AdmissionSafetyError(
                    "The prior scan request cannot be proven; a second scan is refused."
                )
            return {
                "admissionId": int(plan["subjectId"]),
                "bookId": int(plan["bookId"]), "status": status,
                "registered": status == "registered", "scanRequested": False,
                "externalMutationPerformed": False, "libraryBytesChanged": False,
                "stagingRetained": True, "reconciledAfterRestart": True,
            }
        except (
            AdmissionSafetyError, BinderyClientError, OSError, sqlite3.Error,
            StagingSafetyError,
        ) as exc:
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", str(exc)
            ) from exc
        finally:
            _admission_lock.release()


_EXECUTOR = _KnownPublicationRegistrationExecutor()


def register_executor() -> None:
    core.register_automatic_executor(_ACTION, _EXECUTOR)


def run_publication_registration_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    steps = list(plan.get("steps") or [])
    if (int(plan.get("currentStep") or 0) != 5 or len(steps) <= 5
            or steps[5].get("code") != _ACTION):
        return {"ok": True, "state": "paused", "plan": plan,
                "externalMutationAttempted": False}
    try:
        result = core.attempt_automatic_step(int(plan["id"]))
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(int(plan["id"]), str(exc))
        return {"ok": False, "state": "blocked", "plan": blocked,
                "reasonCode": exc.reason_code,
                "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED"}
    external = (result.get("execution") or {}).get("externalResult") or {}
    return {
        **result,
        "state": "reconciled" if result.get("reconciled") else
                 "replayed" if result.get("replayed") else "executed",
        "externalMutationAttempted": bool(external.get("externalMutationPerformed", False)),
    }
