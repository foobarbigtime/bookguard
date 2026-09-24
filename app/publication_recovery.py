"""Publish one verified failed admission after an exact, durable no-overwrite proof."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from . import automatic_execution as core
from . import publication_proof
from .admission import (
    AdmissionSafetyError, _admission_lock, _automation_settings, _book_has_ebook,
    _cleanup_private_snapshot, _copy_stable_snapshot, _destination_for_result,
    _exact_ebook_associations, _identity_unchanged, _publish_no_replace,
    _result_matches_book, _seal_snapshot, _verified_published_destination,
    _verified_staged_source, admission_readiness,
)
from .bindery_client import BinderyClient, BinderyClientError
from .db import ebook_admission_by_id, local_conn, result_by_id, utc_now
from .staging import MIN_ADMISSION_CONFIDENCE, StagingSafetyError, verify_ebook_file


_PROOF_ACTION = "prove_supported_no_replace_method"
_PUBLISH_ACTION = "retry_guarded_publication"
_PROOF_INDEX = 3
_PUBLISH_INDEX = 4
_BOUND_KEYS = ("destinationParent", "parentDevice", "parentInode", "stagedSha256")


def _bound(boundary: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(boundary.get(key) for key in _BOUND_KEYS)


def _proven_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    steps = list(plan.get("steps") or [])
    if (len(steps) < 6 or steps[_PROOF_INDEX].get("code") != _PROOF_ACTION
            or steps[_PUBLISH_INDEX].get("code") != _PUBLISH_ACTION):
        raise core.AutomaticExecutionBlocked(
            "PROOF_STEP_MISMATCH", "The recovery plan does not contain the exact proof."
        )
    receipt = core._existing(str(plan["signature"]), _PROOF_ACTION, _PROOF_INDEX)
    boundary = (receipt or {}).get("boundary") or {}
    outcome = (receipt or {}).get("externalResult") or {}
    if not (
        receipt and receipt.get("state") == "succeeded"
        and boundary.get("ok") is True
        and boundary.get("planSignature") == plan.get("signature")
        and boundary.get("evidenceRevision") == plan.get("evidenceRevision")
        and boundary.get("parentDevice") is not None
        and boundary.get("parentInode") is not None
        and len(str(boundary.get("stagedSha256") or "")) == 64
        and outcome.get("admissionId") == int(plan["subjectId"])
        and outcome.get("publicationMethod") in {"renameat2", "private-snapshot-link"}
        and outcome.get("admissionPublished") is False
        and outcome.get("binderyScanRequested") is False
    ):
        raise core.AutomaticExecutionBlocked(
            "PROOF_NOT_CURRENT", "The exact admission has no complete, current proof."
        )
    return boundary


def _record_publication(plan: dict[str, Any], method: str) -> None:
    with local_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE ebook_admissions
            SET status='published', publication_method=?, failure_stage=NULL,
                error=NULL, updated_at=?
            WHERE id=? AND status='failed' AND failure_stage='no_replace_unsupported'
              AND (publication_method IS NULL OR publication_method='')
              AND staged_sha256=? AND result_id=? AND book_id=? AND stored_path=?
            """,
            (
                method, utc_now(), int(plan["subjectId"]),
                str(plan["stagedSha256"]), int(plan["resultId"]),
                int(plan["bookId"]), str(plan["path"]),
            ),
        )
        if cursor.rowcount != 1:
            raise AdmissionSafetyError("Failed admission state changed before publication was recorded.")
        conn.commit()


class _GuardedPublicationExecutor:
    action_code = _PUBLISH_ACTION

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if step.get("code") != _PUBLISH_ACTION or (
            plan.get("planKind"), plan.get("subjectKind"), plan.get("reasonCode")
        ) != (
            "RECOVER_ADMISSION_PUBLICATION", "admission",
            "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED",
        ):
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "Only one exact failed admission may be published."
            )
        proof = _proven_receipt(plan)
        fresh = publication_proof._EXECUTOR.revalidate(
            plan, {"code": _PROOF_ACTION}
        )
        checks = list(fresh["checks"])
        checks.append({"code": "CURRENT_FILESYSTEM_PROOF", "ok": _bound(fresh) == _bound(proof)})
        return {**fresh, "ok": all(check["ok"] for check in checks),
                "checks": checks, "stagedSha256": fresh["stagedSha256"]}

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        if not _admission_lock.acquire(blocking=False):
            raise AdmissionSafetyError("Another admission operation is already running.")
        private_snapshot: Path | None = None
        try:
            fresh = self.revalidate(plan, step)
            core._require_fresh_boundary(plan, fresh)
            if _bound(fresh) != _bound(boundary):
                raise AdmissionSafetyError("Verified bytes or destination topology changed.")

            admission_id = int(plan["subjectId"])
            admission = ebook_admission_by_id(admission_id)
            result = result_by_id(int(plan["resultId"]))
            staged = _verified_staged_source(admission)
            destination, stored_path = _destination_for_result(result, staged)
            if stored_path != plan["path"]:
                raise AdmissionSafetyError("The Bindery destination mapping changed.")

            client = BinderyClient()
            before = client.get_book(int(plan["bookId"]))
            if not _result_matches_book(result, before) or _book_has_ebook(before):
                raise AdmissionSafetyError("Bindery book identity or ebook state changed.")
            private_snapshot, copied_hash = _copy_stable_snapshot(
                staged, destination.parent, _automation_settings().max_staged_ebook_bytes
            )
            if copied_hash != fresh["stagedSha256"]:
                raise AdmissionSafetyError("Copied snapshot differs from the verified bytes.")
            verification = verify_ebook_file(
                int(plan["bookId"]), private_snapshot,
                display_path=str(admission["staged_relative_path"]), book=before,
            )
            if (
                verification.get("safeToAdmit") is not True
                or int(verification.get("confidence") or 0) < MIN_ADMISSION_CONFIDENCE
                or verification.get("sha256") != copied_hash
            ):
                raise AdmissionSafetyError("Copied snapshot failed fresh ebook verification.")

            after = client.get_book(int(plan["bookId"]))
            if not _identity_unchanged(before, after) or _book_has_ebook(after):
                raise AdmissionSafetyError("Bindery book identity or ebook state changed.")
            current = self.revalidate(plan, step)
            core._require_fresh_boundary(plan, current)
            if _bound(current) != _bound(boundary):
                raise AdmissionSafetyError("Staged bytes or destination topology changed.")
            _seal_snapshot(private_snapshot, copied_hash)
            method = _publish_no_replace(private_snapshot, destination)
            private_snapshot = None

            _record_publication({**plan, "stagedSha256": copied_hash}, method)
            published = ebook_admission_by_id(admission_id)
            _verified_published_destination(published, _automation_settings())
            return {
                "admissionId": admission_id, "bookId": int(plan["bookId"]),
                "status": "published", "publicationMethod": method,
                "stagedSha256": copied_hash, "externalMutationPerformed": True,
                "libraryBytesChanged": True, "stagingRetained": True,
                "binderyScanRequested": False,
            }
        finally:
            _cleanup_private_snapshot(private_snapshot)
            _admission_lock.release()

    def reconcile_uncertain(
        self, plan: dict[str, Any], step: dict[str, Any], existing: dict[str, Any]
    ) -> dict[str, Any]:
        """Adopt only proven published bytes; never retry an interrupted publication."""
        if not _admission_lock.acquire(blocking=False):
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "Another admission operation is running."
            )
        try:
            proof = _proven_receipt(plan)
            attempt_boundary = existing.get("boundary") or {}
            if (
                attempt_boundary.get("planSignature") != plan.get("signature")
                or attempt_boundary.get("evidenceRevision") != plan.get("evidenceRevision")
                or _bound(attempt_boundary) != _bound(proof)
            ):
                raise AdmissionSafetyError("Interrupted publication boundary differs from proof.")
            admission = ebook_admission_by_id(int(plan["subjectId"]))
            result = result_by_id(int(plan["resultId"]))
            if not admission or not result or (
                admission.get("result_id"), admission.get("book_id"),
                admission.get("stored_path"), admission.get("local_path"),
                admission.get("staged_sha256"), result.get("id"),
                result.get("book_id"), result.get("stored_path"),
            ) != (
                plan["resultId"], plan["bookId"], plan["path"],
                result.get("local_path"), proof.get("stagedSha256"),
                plan["resultId"], plan["bookId"], plan["path"],
            ):
                raise AdmissionSafetyError("Interrupted admission identity changed.")
            if admission.get("status") not in {"failed", "published"}:
                raise AdmissionSafetyError("Interrupted admission has an unexpected status.")
            verification = admission.get("verification") or {}
            if not isinstance(verification, dict) or (
                verification.get("safeToAdmit") is not True
                or verification.get("sha256") != proof["stagedSha256"]
            ):
                raise AdmissionSafetyError("Interrupted verified snapshot provenance changed.")
            configured = _automation_settings()
            if not admission_readiness(BinderyClient())["ready"]:
                raise AdmissionSafetyError("Admission readiness changed after interruption.")
            destination = _verified_published_destination(admission, configured)
            _verified_staged_source(admission)
            stat = destination.parent.stat()
            if (str(destination.parent), stat.st_dev, stat.st_ino) != (
                proof["destinationParent"], proof["parentDevice"], proof["parentInode"]
            ):
                raise AdmissionSafetyError("Published filesystem differs from proof.")
            client = BinderyClient()
            book = client.get_book(int(plan["bookId"]))
            if not _result_matches_book(result, book) or _book_has_ebook(book):
                raise AdmissionSafetyError("Bindery identity or ebook state changed.")
            if _exact_ebook_associations(str(plan["path"])):
                raise AdmissionSafetyError("Bindery already owns the exact path.")
            if admission["status"] == "failed":
                _record_publication(
                    {**plan, "stagedSha256": proof["stagedSha256"]},
                    "recovered-after-interruption",
                )
            elif not admission.get("publication_method"):
                raise AdmissionSafetyError("Published admission lacks publication provenance.")
            return {
                "admissionId": int(plan["subjectId"]), "bookId": int(plan["bookId"]),
                "status": "published", "stagedSha256": proof["stagedSha256"],
                "publicationMethod": str(
                    admission.get("publication_method") or "recovered-after-interruption"
                ),
                "externalMutationPerformed": False, "libraryBytesChanged": False,
                "stagingRetained": True, "binderyScanRequested": False,
                "reconciledAfterRestart": True,
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


_EXECUTOR = _GuardedPublicationExecutor()


def register_executor() -> None:
    core.register_automatic_executor(_PUBLISH_ACTION, _EXECUTOR)


def run_publication_recovery_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    index = int(plan.get("currentStep") or 0)
    if index <= _PROOF_INDEX:
        return publication_proof.run_publication_proof_cycle(plan)
    if index != _PUBLISH_INDEX:
        return {"ok": True, "state": "paused", "plan": plan,
                "externalMutationAttempted": False,
                "message": "The later Bindery scan step remains disabled."}
    try:
        result = core.attempt_automatic_step(int(plan["id"]))
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(int(plan["id"]), str(exc))
        return {"ok": False, "state": "blocked", "plan": blocked,
                "reasonCode": exc.reason_code,
                "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED"}
    return {
        **result,
        "state": "reconciled" if result.get("reconciled") else
                 "replayed" if result.get("replayed") else "executed",
        "externalMutationAttempted": not result.get("replayed"),
    }
