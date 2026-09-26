"""Retire one proven failed journal; a later admission uses its existing executor."""

from __future__ import annotations

from typing import Any

from . import automatic_execution as core
from .admission_prepublication_review import prepublication_failure_preview
from .bindery_client import BinderyClient
from .db import ebook_admission_by_id, local_conn, utc_now


_ACTION = "retire_proven_prepublication_failure"
_READ_ONLY_STEPS = ("review_failed_admission", "review_current_handoff")
_BOUND_KEYS = (
    "admissionId", "acquisitionId", "resultId", "bookId", "queueId",
    "planSignature", "evidenceRevision", "stagedRelativePath", "stagedSha256",
    "sourceIdentity", "destination", "binderyPath",
)


def _bound(value: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        tuple(value.get(key) or ()) if key == "sourceIdentity" else value.get(key)
        for key in _BOUND_KEYS
    )


class _PrepublicationRetirementExecutor:
    action_code = _ACTION

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if (plan.get("planKind"), plan.get("subjectKind"), plan.get("reasonCode")) != (
            "REVIEW_ADMISSION_PREPUBLICATION", "admission",
            "ADMISSION_FAILED_BEFORE_PUBLICATION",
        ) or step.get("code") not in (*_READ_ONLY_STEPS, _ACTION):
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "Only an exact failed admission may be retired."
            )
        preview = prepublication_failure_preview(int(plan["subjectId"]), BinderyClient())
        if preview.get("ok") is not True:
            raise core.AutomaticExecutionBlocked(
                str(preview.get("reasonCode") or "PREPUBLICATION_PROOF_FAILED"),
                str(preview.get("message") or "The failed journal is unproven."),
            )
        checks = [
            {"code": "PLAN_IDENTITY_UNCHANGED", "ok": (
                preview["planSignature"] == plan["signature"]
                and preview["evidenceRevision"] == plan["evidenceRevision"]
                and preview["admissionId"] == int(plan["subjectId"])
                and preview["resultId"] == plan["resultId"]
                and preview["bookId"] == plan["bookId"]
                and preview["binderyPath"] == plan["path"]
            )},
            {"code": "FRESH_BYTE_IDENTITY", "ok": (
                len(preview["stagedSha256"]) == 64
                and len(preview["sourceIdentity"]) == 5
            )},
        ]
        return {**preview, "ok": all(item["ok"] for item in checks), "checks": checks}

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        fresh = self.revalidate(plan, step)
        core._require_fresh_boundary(plan, fresh)
        if _bound(fresh) != _bound(boundary):
            raise core.AutomaticExecutionBlocked(
                "PREPUBLICATION_BOUNDARY_CHANGED", "The failed admission proof changed."
            )
        with local_conn() as conn:
            cursor = conn.execute(
                """UPDATE ebook_admissions
                   SET status='retired_before_publication', updated_at=?
                   WHERE id=? AND status='failed'
                     AND failure_stage='before_publication'
                     AND (publication_method IS NULL OR publication_method='')
                     AND result_id=? AND book_id=? AND stored_path=?
                     AND staged_relative_path=?
                     AND (staged_sha256 IS NULL OR staged_sha256=?)
                     AND NOT EXISTS (
                         SELECT 1 FROM ebook_admissions AS other
                         WHERE other.id<>ebook_admissions.id
                           AND (other.result_id=ebook_admissions.result_id
                                OR other.stored_path=ebook_admissions.stored_path)
                     )
                     AND EXISTS (
                         SELECT 1 FROM ebook_acquisitions AS acquisition
                         WHERE acquisition.id=? AND acquisition.status='verified'
                           AND acquisition.admission_id IS NULL
                           AND acquisition.result_id=ebook_admissions.result_id
                           AND acquisition.book_id=ebook_admissions.book_id
                           AND acquisition.queue_id=?
                           AND acquisition.staged_relative_path=?
                           AND acquisition.staged_sha256=?
                     )""",
                (
                    utc_now(), int(plan["subjectId"]), int(plan["resultId"]),
                    int(plan["bookId"]), str(plan["path"]),
                    fresh["stagedRelativePath"], fresh["stagedSha256"],
                    fresh["acquisitionId"],
                    fresh["queueId"], fresh["stagedRelativePath"],
                    fresh["stagedSha256"],
                ),
            )
            if cursor.rowcount != 1:
                raise core.AutomaticExecutionBlocked(
                    "PREPUBLICATION_BOUNDARY_CHANGED",
                    "The journal or acquisition changed before retirement.",
                )
            conn.commit()
        return {
            "admissionId": int(plan["subjectId"]),
            "acquisitionId": fresh["acquisitionId"],
            "status": "retired_before_publication",
            "externalMutationPerformed": False, "localJournalChanged": True,
            "libraryBytesChanged": False, "binderyScanRequested": False,
        }

    def reconcile_uncertain(
        self, plan: dict[str, Any], step: dict[str, Any], existing: dict[str, Any]
    ) -> dict[str, Any]:
        boundary = existing.get("boundary") or {}
        journal = ebook_admission_by_id(int(plan["subjectId"]))
        if (
            existing.get("state") != "running"
            or boundary.get("planSignature") != plan.get("signature")
            or boundary.get("evidenceRevision") != plan.get("evidenceRevision")
            or boundary.get("admissionId") != int(plan["subjectId"])
            or not journal or journal.get("status") != "retired_before_publication"
            or journal.get("failure_stage") != "before_publication"
            or journal.get("publication_method")
            or journal.get("result_id") != plan.get("resultId")
            or journal.get("book_id") != plan.get("bookId")
            or journal.get("stored_path") != plan.get("path")
            or journal.get("staged_relative_path") != boundary.get("stagedRelativePath")
            or journal.get("staged_sha256") not in (None, boundary.get("stagedSha256"))
        ):
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "The retired journal outcome is unproven."
            )
        with local_conn() as conn:
            competing = conn.execute(
                """SELECT 1 FROM ebook_admissions
                   WHERE id<>? AND (result_id=? OR stored_path=?) LIMIT 1""",
                (int(plan["subjectId"]), plan["resultId"], plan["path"]),
            ).fetchone()
            acquisition = conn.execute(
                """SELECT id FROM ebook_acquisitions
                   WHERE id=? AND status='verified' AND admission_id IS NULL
                     AND result_id=? AND book_id=? AND queue_id=?
                     AND staged_relative_path=? AND staged_sha256=?""",
                (boundary.get("acquisitionId"), plan["resultId"], plan["bookId"],
                 boundary.get("queueId"), boundary.get("stagedRelativePath"),
                 boundary.get("stagedSha256")),
            ).fetchone()
        if competing or not acquisition:
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "The retired journal has competing evidence."
            )
        return {
            "admissionId": int(plan["subjectId"]),
            "acquisitionId": boundary["acquisitionId"],
            "status": "retired_before_publication",
            "externalMutationPerformed": False, "localJournalChanged": False,
            "libraryBytesChanged": False, "binderyScanRequested": False,
            "reconciledAfterRestart": True,
        }


EXECUTOR = _PrepublicationRetirementExecutor()


def register_executor() -> None:
    core.register_automatic_executor(_ACTION, EXECUTOR)


def run_prepublication_retirement_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = int(plan["id"])
    for code in _READ_ONLY_STEPS:
        current = core.recovery_plan_by_id(plan_id) or plan
        index = int(current.get("currentStep") or 0)
        steps = list(current.get("steps") or [])
        if index >= len(steps) or steps[index].get("code") != code:
            continue
        try:
            core._require_fresh_boundary(current, EXECUTOR.revalidate(current, steps[index]))
        except core.AutomaticExecutionBlocked as exc:
            blocked = core.block_recovery_plan(plan_id, str(exc))
            return {"ok": False, "state": "blocked", "plan": blocked,
                    "reasonCode": exc.reason_code, "externalMutationAttempted": False}
        plan = core.record_recovery_step_success(plan_id, index)

    current = core.recovery_plan_by_id(plan_id) or plan
    index = int(current.get("currentStep") or 0)
    steps = list(current.get("steps") or [])
    if index >= len(steps) or steps[index].get("code") != _ACTION:
        return {"ok": True, "state": "paused", "plan": current,
                "externalMutationAttempted": False}
    try:
        result = core.attempt_automatic_step(plan_id)
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(plan_id, str(exc))
        return {"ok": False, "state": "blocked", "plan": blocked,
                "reasonCode": exc.reason_code, "externalMutationAttempted": False}
    return {**result, "state": "reconciled" if result.get("reconciled") else
            "replayed" if result.get("replayed") else "executed",
            "externalMutationAttempted": False}
