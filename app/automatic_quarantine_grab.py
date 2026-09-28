"""One selected post-quarantine grab, journaled before remote mutation."""

from __future__ import annotations

from typing import Any

from . import acquisition as workflow
from . import automatic_execution as core
from .alternate_candidate import _candidate_fingerprint
from .automatic_alternate import _acknowledged_queue_id, _prove_queue_after_grab
from .automatic_contracts import AutomaticPostEffectUncertain
from .bindery_client import BinderyClient
from .db import (
    create_ebook_acquisition, ebook_replacement_for_quarantine_plan,
    result_by_id, update_ebook_acquisition,
)
from .quarantine_selection import quarantine_candidate_preview, quarantine_selection_by_result


_ACTION = "reacquire_expected_media"


class _QuarantineGrabExecutor:
    action_code = _ACTION

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if (
            plan.get("planKind") != "QUARANTINE_UNSAFE_MEDIA"
            or plan.get("subjectKind") != "result"
            or plan.get("reasonCode") != "UNSAFE_FILE"
            or int(plan.get("currentStep") or 0) != 3
            or step.get("code") != _ACTION
            or step.get("externalMutation") is not True
        ):
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "The selected quarantine replacement step changed."
            )
        plan_id = int(plan["id"])
        result_id = int(plan["subjectId"])
        selected = quarantine_selection_by_result(result_id)
        if (
            not selected or not selected["currentPlan"]
            or selected["planId"] != plan_id
            or selected["planSignature"] != plan["signature"]
            or selected["evidenceRevision"] != plan["evidenceRevision"]
        ):
            raise core.AutomaticExecutionBlocked(
                "QUARANTINE_CHOICE_STALE", "The exact operator choice or custody is unproven."
            )
        if ebook_replacement_for_quarantine_plan(plan_id):
            raise core.AutomaticExecutionBlocked(
                "REPLACEMENT_ALREADY_STARTED", "A linked replacement already exists."
            )
        try:
            client = BinderyClient()
            preview = quarantine_candidate_preview(
                plan_id, selected["candidate"]["guid"], client
            )
            readiness = workflow.acquisition_readiness(client)
        except workflow.AcquisitionSafetyError as exc:
            raise core.AutomaticExecutionBlocked(
                "QUARANTINE_PREFLIGHT_FAILED", str(exc)
            ) from exc
        if (
            not readiness["ready"]
            or preview["candidateFingerprint"] != selected["candidateFingerprint"]
            or preview["resultId"] != result_id
            or preview["bookId"] != int(plan["bookId"])
            or preview["planSignature"] != plan["signature"]
        ):
            raise core.AutomaticExecutionBlocked(
                "QUARANTINE_PREFLIGHT_FAILED",
                "Readiness or the selected release identity changed: "
                + ", ".join(readiness["blockers"]),
            )
        return {
            "ok": True,
            "planSignature": plan["signature"],
            "evidenceRevision": plan["evidenceRevision"],
            "checks": [
                {"code": "EXACT_QUARANTINE_CUSTODY_AND_CHOICE", "ok": True},
                {"code": "FRESH_RELEASE_AND_READINESS", "ok": True},
                {"code": "NO_LINKED_REPLACEMENT", "ok": True},
            ],
            "candidateGuid": selected["candidate"]["guid"],
            "candidateFingerprint": selected["candidateFingerprint"],
            "quarantineExecutionId": selected["quarantineExecutionId"],
            "quarantineSha256": selected["quarantineSha256"],
        }

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        plan_id = int(plan["id"])
        result_id = int(plan["subjectId"])
        client = BinderyClient()
        with workflow._acquisition_lock:
            selected = quarantine_selection_by_result(result_id)
            if (
                not selected or not selected["currentPlan"]
                or selected["planSignature"] != plan["signature"]
                or selected["quarantineExecutionId"] != boundary["quarantineExecutionId"]
                or selected["quarantineSha256"] != boundary["quarantineSha256"]
                or selected["candidateFingerprint"] != boundary["candidateFingerprint"]
                or ebook_replacement_for_quarantine_plan(plan_id)
            ):
                raise RuntimeError("The chosen release or quarantine custody changed.")
            preview = quarantine_candidate_preview(plan_id, boundary["candidateGuid"], client)
            if preview["candidateFingerprint"] != boundary["candidateFingerprint"]:
                raise RuntimeError("The release changed before grab.")
            result = result_by_id(result_id)
            if not result or int(result["book_id"]) != int(plan["bookId"]):
                raise RuntimeError("The scan result changed before grab.")
            _, title, author = workflow._result_and_book(result, client)
            candidate = workflow._search_candidate(
                client, int(plan["bookId"]), boundary["candidateGuid"], title, author,
            )
            if _candidate_fingerprint(candidate) != boundary["candidateFingerprint"]:
                raise RuntimeError("The release payload changed before grab.")
            readiness = workflow.acquisition_readiness(client)
            if not readiness["ready"]:
                raise RuntimeError("Acquisition readiness changed before grab: "
                                   + ", ".join(readiness["blockers"]))

            child_id = create_ebook_acquisition(
                result, candidate, replacement_for_quarantine_plan_id=plan_id,
            )
            update_ebook_acquisition(child_id, "grab_requested")
            # From this point, any error is uncertain and must retain the running
            # execution record. A failed record would permit a second POST.
            try:
                response = client.grab(int(plan["bookId"]), candidate)
                if isinstance(response, dict) and response.get("accepted") is False:
                    raise workflow.AcquisitionSafetyError("Bindery declined the replacement grab.")
                acknowledged_id = _acknowledged_queue_id(response)
                queue = _prove_queue_after_grab(
                    client, int(plan["bookId"]), str(candidate["title"]),
                    str(candidate.get("protocol") or ""), acknowledged_id,
                )
                queue_id = _acknowledged_queue_id(queue)
                update_ebook_acquisition(
                    child_id, "queued", queue_id=queue_id,
                    grab_response=workflow._safe_grab_response(queue),
                )
            except Exception as exc:
                raise AutomaticPostEffectUncertain(
                    "The replacement grab outcome requires linked queue proof; no replay is allowed. "
                    + str(exc)
                ) from exc
            return {
                "replacementAcquisitionId": child_id, "queueId": queue_id,
                "status": "queued", "externalMutationPerformed": True,
                "admissionAttempted": False,
            }

    def reconcile_uncertain(
        self, plan: dict[str, Any], step: dict[str, Any], existing: dict[str, Any]
    ) -> dict[str, Any]:
        boundary = existing.get("boundary") or {}
        child = ebook_replacement_for_quarantine_plan(int(plan["id"]))
        selected = quarantine_selection_by_result(int(plan["subjectId"]))
        if (
            not child or not selected or existing.get("attemptCount") != 1
            or selected["planId"] != int(plan["id"])
            or selected["planSignature"] != plan["signature"]
            or selected["evidenceRevision"] != plan["evidenceRevision"]
            or selected["quarantineExecutionId"] != boundary.get("quarantineExecutionId")
            or selected["quarantineSha256"] != boundary.get("quarantineSha256")
            or selected["candidateFingerprint"] != boundary.get("candidateFingerprint")
            or child["candidate_guid"] != boundary.get("candidateGuid")
            or child["candidate_title"] != selected["candidate"]["title"]
            or str(child["candidate_protocol"] or "") != selected["candidate"]["protocol"]
            or int(child["result_id"]) != int(plan["subjectId"])
            or int(child["book_id"]) != int(plan["bookId"])
            or child["admission_id"] is not None
            or child["status"] not in {"grab_requested", "queued", "downloading", "awaiting_staging"}
        ):
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "The interrupted replacement child is unproven."
            )
        client = BinderyClient()
        try:
            queue = _prove_queue_after_grab(
                client, int(child["book_id"]), str(child["candidate_title"]),
                str(child["candidate_protocol"] or ""),
            )
            queue_id = _acknowledged_queue_id(queue)
            if child["queue_id"] is not None and int(child["queue_id"]) != queue_id:
                raise workflow.AcquisitionSafetyError("The linked queue identity changed.")
            reconciled = workflow.reconcile_ebook_acquisition(int(child["id"]), client)
            after = reconciled.get("acquisition") or {}
            if (
                int(after.get("queue_id") or 0) != queue_id
                or after.get("status") not in {"queued", "downloading", "awaiting_staging"}
            ):
                raise workflow.AcquisitionSafetyError("The queue proof did not reconcile.")
        except Exception as exc:
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", str(exc)
            ) from exc
        return {
            "replacementAcquisitionId": int(child["id"]), "queueId": queue_id,
            "status": str(after["status"]), "externalMutationPerformed": False,
            "admissionAttempted": False, "reconciledAfterRestart": True,
        }


_EXECUTOR = _QuarantineGrabExecutor()


def register_executor() -> None:
    core.register_automatic_executor(_ACTION, _EXECUTOR)


def run_quarantine_grab_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    try:
        result = core.attempt_automatic_step(int(plan["id"]))
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        if exc.reason_code == "POST_EFFECT_UNCERTAIN":
            return {
                "ok": False, "state": "waiting", "plan": core.recovery_plan_by_id(int(plan["id"])),
                "externalMutationAttempted": True, "reasonCode": exc.reason_code,
                "message": str(exc),
            }
        blocked = core.block_recovery_plan(int(plan["id"]), str(exc))
        return {
            "ok": False, "state": "blocked", "plan": blocked,
            "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED",
            "reasonCode": exc.reason_code, "message": str(exc),
        }
    return {
        **result, "state": "reconciled" if result.get("reconciled") else "executed",
        "externalMutationAttempted": not result.get("replayed", False),
    }
