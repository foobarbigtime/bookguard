"""One supervised alternate grab, with durable child identity and no replay."""

from __future__ import annotations

from typing import Any

from . import acquisition as workflow
from . import automatic_execution as core
from .acquisition_progress import _verified_snapshot_matches
from .alternate_candidate import _candidate_fingerprint, alternate_candidate_preview
from .alternate_selection import alternate_selection_by_acquisition
from .bindery_client import BinderyClient
from .db import (
    create_ebook_acquisition,
    ebook_replacement_for_acquisition,
    result_by_id,
    update_ebook_acquisition,
)
from .file_safety import sha256_file
from .staging import StagingSafetyError, list_staged_ebooks, resolve_staged_file


_ACTION = "request_alternate_grab"
_PLAN = "SELECT_ALTERNATE_REPLACEMENT"


def _prove_queue_after_grab(
    client: BinderyClient, book_id: int, title: str, protocol: str,
    acknowledged_id: int | None = None,
) -> dict[str, Any]:
    """Prove one alternate against a complete live queue snapshot."""
    items, partial = workflow._queue_payload(client)
    if partial:
        raise workflow.AcquisitionSafetyError("Bindery queue is partial.")
    matches = [
        item for item in workflow._active_or_unknown_queue_items(items)
        if str(item.get("bookId") or "") == str(book_id)
    ]
    if len(matches) != 1:
        raise workflow.AcquisitionSafetyError(
            "Exactly one current queue item is required after the grab."
        )
    queue = matches[0]
    try:
        queue_id = _acknowledged_queue_id({"id": queue.get("id")})
        item_id = _acknowledged_queue_id(queue)
    except workflow.AcquisitionSafetyError as exc:
        raise workflow.AcquisitionSafetyError(
            "The post-grab queue identity is unproven."
        ) from exc
    if (
        queue_id is None or queue_id <= 0
        or item_id != queue_id
        or (acknowledged_id is not None and acknowledged_id != queue_id)
        or str(queue.get("title") or "").strip().casefold()
        != title.strip().casefold()
        or str(queue.get("protocol") or "").strip().casefold()
        != protocol.strip().casefold()
    ):
        raise workflow.AcquisitionSafetyError(
            "The post-grab queue identity is unproven."
        )
    return queue


def _acknowledged_queue_id(response: Any) -> int | None:
    """Require all IDs in a grab acknowledgement to agree before queue adoption."""
    if not isinstance(response, dict):
        return None
    ids: set[int] = set()
    for source in (response, *(response.get(key) for key in ("item", "queueItem", "queue"))):
        if not isinstance(source, dict):
            continue
        for key in ("id", "queueId"):
            raw = source.get(key)
            if raw is None:
                continue
            if not (
                isinstance(raw, int) and not isinstance(raw, bool)
                or isinstance(raw, str) and raw.strip().isascii()
                and raw.strip().isdecimal()
            ):
                raise workflow.AcquisitionSafetyError("The grab acknowledgement ID is invalid.")
            if isinstance(raw, str) and len(raw.strip()) > 19:
                raise workflow.AcquisitionSafetyError("The grab acknowledgement ID is invalid.")
            parsed = int(raw)
            if not 0 < parsed <= 2**63 - 1:
                raise workflow.AcquisitionSafetyError("The grab acknowledgement ID is invalid.")
            ids.add(parsed)
    if len(ids) > 1:
        raise workflow.AcquisitionSafetyError("The grab acknowledgement IDs conflict.")
    return next(iter(ids), None)


class _AlternateGrabExecutor:
    action_code = _ACTION

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if (
            plan.get("planKind") != _PLAN
            or plan.get("reasonCode") != "ACQUISITION_RELEASE_ALREADY_IMPORTED"
            or plan.get("subjectKind") != "acquisition"
            or step.get("code") != _ACTION
        ):
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "The alternate grab plan or step changed."
            )
        parent_id = int(plan["subjectId"])
        selected = alternate_selection_by_acquisition(parent_id)
        if (
            not selected
            or selected["planId"] != plan["id"]
            or selected["planSignature"] != plan["signature"]
            or selected["evidenceRevision"] != plan["evidenceRevision"]
            or not selected["currentPlan"]
        ):
            raise core.AutomaticExecutionBlocked(
                "ALTERNATE_CHOICE_STALE", "No exact current operator choice is bound to this plan."
            )
        if ebook_replacement_for_acquisition(parent_id):
            raise core.AutomaticExecutionBlocked(
                "REPLACEMENT_ALREADY_STARTED", "A linked replacement already exists."
            )
        try:
            preview = alternate_candidate_preview(
                parent_id, selected["candidate"]["guid"]
            )
        except workflow.AcquisitionSafetyError as exc:
            raise core.AutomaticExecutionBlocked(
                "ALTERNATE_PREFLIGHT_FAILED", str(exc)
            ) from exc
        if (
            preview["candidateFingerprint"] != selected["candidateFingerprint"]
            or preview["resultId"] != plan["resultId"]
            or preview["bookId"] != plan["bookId"]
        ):
            raise core.AutomaticExecutionBlocked(
                "ALTERNATE_IDENTITY_CHANGED", "The chosen release or book changed."
            )
        return {
            "ok": True,
            "planSignature": plan["signature"],
            "evidenceRevision": plan["evidenceRevision"],
            "checks": [
                {"code": "CURRENT_PLAN_AND_SELECTION", "ok": True},
                {"code": "FRESH_READINESS_AND_CANDIDATE", "ok": True},
                {"code": "NO_LINKED_REPLACEMENT", "ok": True},
            ],
            "acquisitionId": parent_id,
            "candidateGuid": selected["candidate"]["guid"],
            "candidateFingerprint": selected["candidateFingerprint"],
        }

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        parent_id = int(plan["subjectId"])
        client = BinderyClient()
        with workflow._acquisition_lock:
            selected = alternate_selection_by_acquisition(parent_id)
            if (
                not selected or not selected["currentPlan"]
                or selected["planSignature"] != plan["signature"]
                or selected["candidateFingerprint"] != boundary["candidateFingerprint"]
                or ebook_replacement_for_acquisition(parent_id)
            ):
                raise RuntimeError("The operator choice or replacement state changed.")
            try:
                preview = alternate_candidate_preview(
                    parent_id, selected["candidate"]["guid"], client
                )
                if preview["candidateFingerprint"] != boundary["candidateFingerprint"]:
                    raise workflow.AcquisitionSafetyError("The release payload changed.")
                result = result_by_id(int(plan["resultId"]))
                if not result or int(result["book_id"]) != int(plan["bookId"]):
                    raise workflow.AcquisitionSafetyError("The scan result changed.")
                _, title, author = workflow._result_and_book(result, client)
                candidate = workflow._search_candidate(
                    client, int(plan["bookId"]),
                    selected["candidate"]["guid"], title, author,
                )
                # A second search must still represent the same exact grab payload.
                if _candidate_fingerprint(candidate) != boundary["candidateFingerprint"]:
                    raise workflow.AcquisitionSafetyError("The release changed before grab.")
                # Bindery can accept other work during a candidate search.
                readiness = workflow.acquisition_readiness(client)
                if not readiness["ready"]:
                    raise workflow.AcquisitionSafetyError(
                        "Acquisition readiness changed before alternate grab: "
                        + ", ".join(readiness["blockers"])
                    )
            except workflow.AcquisitionSafetyError as exc:
                raise RuntimeError(str(exc)) from exc

            child_id = create_ebook_acquisition(
                result, candidate, replacement_for_acquisition_id=parent_id,
            )
            update_ebook_acquisition(child_id, "grab_requested")
            # Any exception after this point has an uncertain remote outcome.
            # The runner blocks rather than submitting a second grab.
            response = client.grab(int(plan["bookId"]), candidate)
            if isinstance(response, dict) and response.get("accepted") is False:
                raise workflow.AcquisitionSafetyError("Bindery declined the alternate grab.")
            # Even a response with an ID needs independent attribution. After
            # this POST, any missing or conflicting proof blocks without replay.
            acknowledged_id = _acknowledged_queue_id(response)
            queue = _prove_queue_after_grab(
                client, int(plan["bookId"]),
                str(candidate["title"]), str(candidate.get("protocol") or ""),
                acknowledged_id,
            )
            queue_id = _acknowledged_queue_id(queue)
            grab_response = workflow._safe_grab_response(queue)
            update_ebook_acquisition(
                child_id, "queued",
                queue_id=queue_id,
                grab_response=grab_response,
            )
            return {
                "parentAcquisitionId": parent_id,
                "replacementAcquisitionId": child_id,
                "status": "queued",
                "queueId": queue_id,
                "externalMutationPerformed": True,
                "admissionAttempted": False,
            }

    def reconcile_uncertain(
        self, plan: dict[str, Any], step: dict[str, Any], existing: dict[str, Any]
    ) -> dict[str, Any]:
        parent_id = int(plan["subjectId"])
        boundary = existing.get("boundary") or {}
        selected = alternate_selection_by_acquisition(parent_id)
        child = ebook_replacement_for_acquisition(parent_id)
        if (
            not selected or not child
            or selected["planSignature"] != plan["signature"]
            or selected["candidateFingerprint"] != boundary.get("candidateFingerprint")
            or child["candidate_guid"] != boundary.get("candidateGuid")
            or child["candidate_title"] != selected["candidate"]["title"]
            or str(child["candidate_protocol"] or "") != selected["candidate"]["protocol"]
            or int(child["result_id"]) != int(plan["resultId"])
            or int(child["book_id"]) != int(plan["bookId"])
            or child["admission_id"] is not None
            or child["status"] not in {
                "grab_requested", "queued", "downloading", "awaiting_staging",
                "staging_observed", "verified",
            }
        ):
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "The interrupted child identity is unproven."
            )
        client = BinderyClient()
        try:
            queue = _prove_queue_after_grab(
                client, int(child["book_id"]),
                str(child["candidate_title"]), str(child["candidate_protocol"] or ""),
            )
            queue_id = _acknowledged_queue_id(queue)
            if child["queue_id"] is not None and queue_id != child["queue_id"]:
                raise workflow.AcquisitionSafetyError("The linked queue identity changed.")
            if child["status"] == "verified":
                if child["queue_id"] is None:
                    raise workflow.AcquisitionSafetyError(
                        "The verified alternate has no durable queue identity."
                    )
                if workflow._queue_status(queue) not in workflow._AWAITING_STAGING_QUEUE_STATUSES:
                    raise workflow.AcquisitionSafetyError(
                        "The verified alternate has no completed queue handoff."
                    )
                inventory = list_staged_ebooks(1000)
                staged = inventory.get("items") or []
                if inventory.get("truncated") or len(staged) != 1:
                    raise workflow.AcquisitionSafetyError(
                        "The verified alternate has no unique staged source."
                    )
                fingerprint = (
                    str(staged[0]["relativePath"]), int(staged[0]["size"]),
                    int(staged[0]["modifiedNs"]),
                )
                _, staged_path = resolve_staged_file(fingerprint[0])
                staged_hash = sha256_file(staged_path)
                if (
                    child.get("staged_relative_path") != fingerprint[0]
                    or child.get("staged_sha256") != staged_hash
                    or not _verified_snapshot_matches(
                        child, int(plan["bookId"]), fingerprint, staged_hash,
                    )
                ):
                    raise workflow.AcquisitionSafetyError(
                        "The verified alternate's staged evidence is unproven."
                    )
            reconciled = workflow.reconcile_ebook_acquisition(int(child["id"]), client)
        except (workflow.AcquisitionSafetyError, StagingSafetyError, OSError, ValueError) as exc:
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", str(exc)
            ) from exc
        after = reconciled.get("acquisition") or {}
        if (
            str(after.get("queue_id") or "") != str(queue_id)
            or str(after.get("status") or "") not in {
                "queued", "downloading", "awaiting_staging", "staging_observed", "verified",
            }
        ):
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "The queue proof did not reconcile."
            )
        return {
            "parentAcquisitionId": parent_id,
            "replacementAcquisitionId": int(child["id"]),
            "status": str(after["status"]),
            "externalMutationPerformed": False,
            "admissionAttempted": False,
            "reconciledAfterRestart": True,
        }


_EXECUTOR = _AlternateGrabExecutor()


def register_executor() -> None:
    core.register_automatic_executor(_ACTION, _EXECUTOR)


def run_alternate_grab_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = int(plan["id"])
    if int(plan.get("currentStep") or 0) <= 3:
        choice = alternate_selection_by_acquisition(int(plan["subjectId"]))
        if not choice:
            return {
                "ok": True, "state": "waiting", "plan": plan,
                "externalMutationAttempted": False,
                "message": "An explicit alternate candidate must be selected first.",
            }
    for code in (
        "revalidate_failed_acquisition", "refresh_history_and_candidates",
        "select_alternate_candidate",
    ):
        refreshed = core.recovery_plan_by_id(plan_id) or plan
        index = int(refreshed.get("currentStep") or 0)
        steps = refreshed.get("steps") or []
        if index >= len(steps) or steps[index].get("code") != code:
            continue
        try:
            boundary = _EXECUTOR.revalidate(
                refreshed, {"code": _ACTION}
            )
            core._require_fresh_boundary(refreshed, boundary)
        except core.AutomaticExecutionBlocked as exc:
            blocked = core.block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False, "state": "blocked", "plan": blocked,
                "externalMutationAttempted": False,
                "reasonCode": exc.reason_code, "message": str(exc),
            }
        plan = core.record_recovery_step_success(plan_id, index)

    refreshed = core.recovery_plan_by_id(plan_id) or plan
    index = int(refreshed.get("currentStep") or 0)
    steps = refreshed.get("steps") or []
    if index >= len(steps) or steps[index].get("code") != _ACTION:
        return {
            "ok": True, "state": "paused", "plan": refreshed,
            "externalMutationAttempted": False,
            "message": "The alternate grab step is complete; later steps remain separate.",
        }
    try:
        result = core.attempt_automatic_step(plan_id)
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(plan_id, str(exc))
        return {
            "ok": False, "state": "blocked", "plan": blocked,
            "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED",
            "reasonCode": exc.reason_code, "message": str(exc),
        }
    return {
        **result,
        "state": "reconciled" if result.get("reconciled") else "executed",
        "externalMutationAttempted": not result.get("replayed", False),
    }
