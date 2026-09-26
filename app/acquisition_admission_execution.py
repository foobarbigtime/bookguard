"""One allowlisted verified-acquisition admission with no publication replay."""

from __future__ import annotations

from typing import Any

from . import automatic_execution as core
from .acquisition import (
    AcquisitionSafetyError, _AWAITING_STAGING_QUEUE_STATUSES, _queue_payload,
    _queue_status, admit_ebook_acquisition,
)
from .acquisition_admission_preflight import (
    _source_identity, acquisition_admission_preview,
)
from .acquisition_progress import _queue_identity
from .admission import (
    AdmissionSafetyError, _book_has_ebook, _result_matches_book,
    admission_readiness, admission_reconcile_preview,
)
from .bindery_client import BinderyClient, BinderyClientError
from .config import load_automation_settings, settings
from .db import ebook_acquisition_by_id, local_conn, result_by_id, utc_now
from .file_snapshot import SnapshotError, stable_file_fingerprint
from .observe import _acquisition_decisions
from .recovery_planner import _build_plan
from .staging import resolve_staged_file


_ACTION = "admit_verified_acquisition"
_BOUND_KEYS = (
    "acquisitionId", "resultId", "bookId", "queueId", "planSignature",
    "evidenceRevision", "stagedRelativePath", "stagedSha256",
    "sourceIdentity", "destination", "binderyPath",
)


def _bound(value: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        tuple(value.get(key) or ()) if key == "sourceIdentity" else value.get(key)
        for key in _BOUND_KEYS
    )


class _VerifiedAcquisitionAdmissionExecutor:
    action_code = _ACTION

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if (
            plan.get("planKind"), plan.get("subjectKind"), plan.get("reasonCode")
        ) != (
            "PREPARE_ACQUISITION_ADMISSION", "acquisition",
            "VERIFIED_ACQUISITION_REVIEW_AVAILABLE",
        ) or step.get("code") not in {"review_verified_acquisition", _ACTION}:
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "Only an exact verified acquisition may be admitted."
            )
        acquisition_id = int(plan["subjectId"])
        preview = acquisition_admission_preview(acquisition_id, BinderyClient())
        if preview.get("ok") is not True:
            raise core.AutomaticExecutionBlocked(
                str(preview.get("reasonCode") or "ADMISSION_PREFLIGHT_FAILED"),
                str(preview.get("message") or "Admission preflight is unproven."),
            )
        checks = [
            {"code": "SUBJECT_IDENTITY_UNCHANGED", "ok": (
                preview["acquisitionId"] == acquisition_id
                and preview["resultId"] == plan["resultId"]
                and preview["bookId"] == plan["bookId"]
                and preview["queueId"] is not None
            )},
            {"code": "PATH_UNCHANGED", "ok": (
                preview["stagedRelativePath"] == plan["path"]
            )},
            {"code": "EVIDENCE_REVISION_UNCHANGED", "ok": (
                preview["evidenceRevision"] == plan["evidenceRevision"]
            )},
            {"code": "DECISION_STILL_AUTHORIZED", "ok": (
                preview["planSignature"] == plan["signature"]
            )},
            {"code": "WORKFLOW_STATE_UNCHANGED", "ok": (
                len(preview["sourceIdentity"]) == 5
                and len(preview["stagedSha256"]) == 64
            )},
        ]
        return {
            **preview, "ok": all(check["ok"] for check in checks), "checks": checks,
        }

    def _before_publish(
        self, plan: dict[str, Any], boundary: dict[str, Any], admission_id: int,
    ) -> None:
        """Recheck live authorization after the private snapshot, just before publication."""
        configured = load_automation_settings()
        if (
            configured.automation_mode != "automatic"
            or _ACTION not in configured.automatic_action_allowlist
            or not configured.automatic_reacquisition
            or not settings.allow_actions
            or not configured.admission_enabled
        ):
            raise AdmissionSafetyError("Live admission was disabled before publication.")
        acquisition = ebook_acquisition_by_id(int(plan["subjectId"]))
        result = result_by_id(int(plan["resultId"]))
        with local_conn() as conn:
            decision = next((
                item for item in _acquisition_decisions(conn, 2000)
                if str(item.get("subjectId")) == str(plan["subjectId"])
                and item.get("subjectKind") == "acquisition"
            ), None)
            current = _build_plan(conn, decision) if decision else None
            recorded = conn.execute(
                "SELECT state, signature, evidence_revision FROM recovery_plans WHERE id=?",
                (int(plan["id"]),),
            ).fetchone()
            journal = conn.execute(
                "SELECT * FROM ebook_admissions WHERE id=?", (admission_id,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM ebook_admissions WHERE result_id=?",
                (int(plan["resultId"]),),
            ).fetchone()[0]
            running = conn.execute(
                """SELECT state FROM automatic_executions
                   WHERE plan_signature=? AND action_code=? AND step_index=1""",
                (str(plan["signature"]), _ACTION),
            ).fetchone()
        if (
            not acquisition or acquisition.get("status") != "verified"
            or acquisition.get("admission_id") is not None
            or acquisition.get("result_id") != plan["resultId"]
            or acquisition.get("book_id") != plan["bookId"]
            or acquisition.get("queue_id") != boundary["queueId"]
            or acquisition.get("staged_relative_path") != boundary["stagedRelativePath"]
            or acquisition.get("staged_sha256") != boundary["stagedSha256"]
            or not result or result.get("book_id") != plan["bookId"]
            or result.get("stored_path") != boundary["binderyPath"]
            or not current or current.get("signature") != plan["signature"]
            or current.get("evidenceRevision") != plan["evidenceRevision"]
            or not recorded or recorded["state"] not in {"planned", "ready"}
            or recorded["signature"] != plan["signature"]
            or recorded["evidence_revision"] != plan["evidenceRevision"]
            or not journal or journal["status"] != "verified"
            or journal["result_id"] != plan["resultId"]
            or journal["book_id"] != plan["bookId"]
            or journal["stored_path"] != boundary["binderyPath"]
            or journal["local_path"] != result["local_path"]
            or journal["staged_relative_path"] != boundary["stagedRelativePath"]
            or journal["staged_sha256"] != boundary["stagedSha256"]
            or count != 1 or not running or running["state"] != "running"
        ):
            raise AdmissionSafetyError("Durable admission authorization changed before publication.")
        client = BinderyClient()
        items, partial = _queue_payload(client)
        queue, exact, no_competing = _queue_identity(acquisition, items)
        if partial or not exact or not no_competing or (
            _queue_status(queue) not in _AWAITING_STAGING_QUEUE_STATUSES
        ):
            raise AdmissionSafetyError("Exact Bindery queue handoff changed before publication.")
        _, staged_path = resolve_staged_file(boundary["stagedRelativePath"])
        source = list(_source_identity(staged_path))
        if source != boundary["sourceIdentity"] or stable_file_fingerprint(
            staged_path, max_bytes=source[2],
        ) != f"sha256:{boundary['stagedSha256']}:{source[2]}":
            raise AdmissionSafetyError("Staged source changed before publication.")
        book = client.get_book(int(plan["bookId"]))
        if _book_has_ebook(book) or not _result_matches_book(result, book):
            raise AdmissionSafetyError("Bindery book changed before publication.")
        if not admission_readiness(client)["ready"]:
            raise AdmissionSafetyError("Admission readiness changed before publication.")

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        # The running receipt is durable before this second live boundary check.
        fresh = self.revalidate(plan, step)
        core._require_fresh_boundary(plan, fresh)
        if _bound(fresh) != _bound(boundary):
            raise AcquisitionSafetyError("Admission boundary changed before publication.")
        def before_publish(admission_id: int) -> None:
            try:
                self._before_publish(plan, boundary, admission_id)
            except Exception as exc:
                raise AdmissionSafetyError(str(exc)) from exc

        result = admit_ebook_acquisition(
            int(plan["subjectId"]), BinderyClient(),
            before_publish=before_publish, request_scan=False,
        )
        admission = result.get("admission") or {}
        acquisition = result.get("acquisition") or {}
        if (
            acquisition.get("status") != "admitted"
            or acquisition.get("admission_id") != admission.get("admissionId")
            or admission.get("sha256") != boundary["stagedSha256"]
            or admission.get("relativePath") != boundary["stagedRelativePath"]
            or admission.get("binderyPath") != boundary["binderyPath"]
            or admission.get("libraryPath") != boundary["destination"]
        ):
            raise AcquisitionSafetyError(
                "Guarded admission returned a contradictory durable outcome."
            )
        return {
            "admissionId": int(admission["admissionId"]),
            "acquisitionId": int(plan["subjectId"]),
            "bookId": int(plan["bookId"]),
            "status": str(admission["status"]),
            "stagedSha256": str(admission["sha256"]),
            "publicationMethod": str(admission["publicationMethod"]),
            "externalMutationPerformed": True,
            "libraryBytesChanged": True,
            "scanRequested": False,
            "stagingRetained": True,
        }

    def reconcile_uncertain(
        self, plan: dict[str, Any], step: dict[str, Any], existing: dict[str, Any]
    ) -> dict[str, Any]:
        """Adopt a proven journal and published bytes; never call admission or scan."""
        try:
            boundary = existing.get("boundary") or {}
            if (
                existing.get("state") != "running"
                or boundary.get("planSignature") != plan.get("signature")
                or boundary.get("evidenceRevision") != plan.get("evidenceRevision")
                or boundary.get("acquisitionId") != int(plan["subjectId"])
                or boundary.get("resultId") != plan.get("resultId")
                or boundary.get("bookId") != plan.get("bookId")
                or boundary.get("stagedRelativePath") != plan.get("path")
                or len(str(boundary.get("stagedSha256") or "")) != 64
                or len(boundary.get("sourceIdentity") or []) != 5
            ):
                raise AdmissionSafetyError("Interrupted admission boundary is incomplete.")

            acquisition = ebook_acquisition_by_id(int(plan["subjectId"]))
            result = result_by_id(int(plan["resultId"]))
            if not acquisition or not result or (
                acquisition.get("status") not in {"verified", "admitted"}
                or acquisition.get("result_id") != plan["resultId"]
                or acquisition.get("book_id") != plan["bookId"]
                or acquisition.get("queue_id") != boundary["queueId"]
                or acquisition.get("staged_relative_path") != boundary["stagedRelativePath"]
                or acquisition.get("staged_sha256") != boundary["stagedSha256"]
                or result.get("book_id") != plan["bookId"]
                or result.get("stored_path") != boundary["binderyPath"]
                or result.get("format") != "ebook"
            ):
                raise AdmissionSafetyError("Interrupted acquisition identity changed.")

            client = BinderyClient()
            items, partial = _queue_payload(client)
            queue, exact, no_competing = _queue_identity(acquisition, items)
            if partial or not exact or not no_competing or (
                _queue_status(queue) not in _AWAITING_STAGING_QUEUE_STATUSES
            ):
                raise AdmissionSafetyError("Interrupted queue handoff is unproven.")

            with local_conn() as conn:
                rows = conn.execute(
                    "SELECT id FROM ebook_admissions WHERE result_id=?",
                    (int(plan["resultId"]),),
                ).fetchall()
            if len(rows) != 1:
                raise AdmissionSafetyError("Exactly one admission journal is required.")
            admission_id = int(rows[0]["id"])
            preview = admission_reconcile_preview(
                admission_id, client, allow_published_without_scan=True,
            )
            admission = preview if preview.get("safe") else None
            if not admission or preview.get("status") != "published" or (
                preview.get("registrationState") != "scan_required"
            ):
                raise AdmissionSafetyError("Published admission outcome is unproven.")

            with local_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM ebook_admissions WHERE id=?", (admission_id,),
                ).fetchone()
            journal = dict(row) if row else {}
            verification = {}
            if journal.get("verification_json"):
                import json
                verification = json.loads(journal["verification_json"])
            if not (
                journal.get("result_id") == plan["resultId"]
                and journal.get("book_id") == plan["bookId"]
                and journal.get("scan_id") == acquisition.get("scan_id")
                and journal.get("staged_relative_path") == boundary["stagedRelativePath"]
                and journal.get("staged_sha256") == boundary["stagedSha256"]
                and journal.get("stored_path") == boundary["binderyPath"]
                and journal.get("local_path") == result.get("local_path")
                and journal.get("publication_method")
                and isinstance(verification, dict)
                and verification.get("safeToAdmit") is True
                and verification.get("sha256") == boundary["stagedSha256"]
                and preview.get("stagedSha256") == boundary["stagedSha256"]
                and preview.get("storedPath") == boundary["binderyPath"]
                and preview.get("publishedPath") == boundary["destination"]
                and list(_source_identity(preview["stagedPath"]))
                == boundary["sourceIdentity"]
            ):
                raise AdmissionSafetyError("Published admission provenance changed.")

            if acquisition["status"] == "verified":
                with local_conn() as conn:
                    cursor = conn.execute(
                        """UPDATE ebook_acquisitions
                           SET status='admitted', admission_id=?, updated_at=?
                           WHERE id=? AND status='verified' AND admission_id IS NULL
                             AND result_id=? AND book_id=? AND queue_id=?
                             AND staged_relative_path=? AND staged_sha256=?""",
                        (
                            admission_id, utc_now(), int(plan["subjectId"]),
                            int(plan["resultId"]), int(plan["bookId"]), boundary["queueId"],
                            boundary["stagedRelativePath"], boundary["stagedSha256"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise AdmissionSafetyError("Acquisition changed before adoption.")
                    conn.commit()
            elif acquisition.get("admission_id") != admission_id:
                raise AdmissionSafetyError("Acquisition links a different admission.")

            return {
                "admissionId": admission_id, "acquisitionId": int(plan["subjectId"]),
                "bookId": int(plan["bookId"]), "status": preview["status"],
                "stagedSha256": boundary["stagedSha256"],
                "publicationMethod": journal["publication_method"],
                "externalMutationPerformed": False, "libraryBytesChanged": False,
                "scanRequested": False, "stagingRetained": True,
                "reconciledAfterRestart": True,
            }
        except (
            AcquisitionSafetyError, AdmissionSafetyError, BinderyClientError,
            SnapshotError, OSError, ValueError, TypeError, KeyError,
        ) as exc:
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", str(exc)
            ) from exc


EXECUTOR = _VerifiedAcquisitionAdmissionExecutor()


def register_executor() -> None:
    core.register_automatic_executor(_ACTION, EXECUTOR)


def run_verified_admission_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = int(plan["id"])
    steps = list(plan.get("steps") or [])
    index = int(plan.get("currentStep") or 0)
    if index == 0 and steps[0].get("code") == "review_verified_acquisition":
        try:
            boundary = EXECUTOR.revalidate(plan, steps[0])
            core._require_fresh_boundary(plan, boundary)
        except core.AutomaticExecutionBlocked as exc:
            blocked = core.block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False, "state": "blocked", "plan": blocked,
                "reasonCode": exc.reason_code, "externalMutationAttempted": False,
            }
        plan = core.record_recovery_step_success(plan_id, 0)
        steps = list(plan.get("steps") or [])
        index = int(plan.get("currentStep") or 0)
    if index != 1 or len(steps) != 2 or steps[index].get("code") != _ACTION:
        return {
            "ok": True, "state": "paused", "plan": plan,
            "externalMutationAttempted": False,
            "message": "No verified admission mutation step is current.",
        }
    try:
        result = core.attempt_automatic_step(plan_id)
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(plan_id, str(exc))
        return {
            "ok": False, "state": "blocked", "plan": blocked,
            "reasonCode": exc.reason_code,
            "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED",
        }
    execution = dict((result.get("execution") or {}).get("externalResult") or {})
    return {
        **result,
        "state": "reconciled" if result.get("reconciled") else
                 "replayed" if result.get("replayed") else "executed",
        "externalMutationAttempted": bool(
            execution.get("externalMutationPerformed", not result.get("replayed"))
        ),
    }
