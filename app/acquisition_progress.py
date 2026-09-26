"""Supervised progression of a known queue handoff into staged verification."""

from __future__ import annotations

from typing import Any

from . import automatic_execution as core
from .acquisition import (
    AcquisitionSafetyError,
    _AWAITING_STAGING_QUEUE_STATUSES,
    _active_or_unknown_queue_items,
    _queue_payload,
    _queue_status,
    reconcile_ebook_acquisition,
)
from .bindery_client import BinderyClient
from .db import ebook_acquisition_by_id, local_conn
from .file_safety import sha256_file
from .observe import _acquisition_decisions
from .recovery_planner import _build_plan
from .staging import StagingSafetyError, list_staged_ebooks, resolve_staged_file


def _queue_identity(
    acquisition: dict[str, Any], items: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool, bool]:
    """Check the exact handoff and competing active work against one queue read."""
    queue_id = acquisition.get("queue_id")
    exact = [
        item for item in items
        if queue_id is not None and str(item.get("id") or "") == str(queue_id)
    ]
    queue = exact[0] if len(exact) == 1 else {}
    exact_identity = (
        isinstance(queue_id, int) and not isinstance(queue_id, bool) and queue_id > 0
        and len(exact) == 1
        and (queue.get("queueId") is None
             or str(queue["queueId"]) == str(queue_id))
        and str(queue.get("bookId") or "") == str(acquisition["book_id"])
        and bool(str(acquisition.get("candidate_title") or "").strip())
        and bool(str(acquisition.get("candidate_protocol") or "").strip())
        and str(queue.get("title") or "").strip().casefold()
        == str(acquisition.get("candidate_title") or "").strip().casefold()
        and str(queue.get("protocol") or "").strip().casefold()
        == str(acquisition.get("candidate_protocol") or "").strip().casefold()
    )
    no_competing = not any(
        str(item.get("bookId") or "") == str(acquisition["book_id"])
        and str(item.get("id") or "") != str(queue_id)
        for item in _active_or_unknown_queue_items(items)
    )
    return queue, exact_identity, no_competing


def _verified_snapshot_matches(
    acquisition: dict[str, Any], book_id: int, fingerprint: tuple, staged_hash: str,
) -> bool:
    verification = acquisition.get("verification")
    return bool(
        isinstance(verification, dict)
        and verification.get("safeToAdmit") is True
        and verification.get("stableDuringVerification") is True
        and verification.get("verdict") == "VERIFIED_CORRECT"
        and verification.get("bookId") == book_id
        and verification.get("relativePath") == fingerprint[0]
        and verification.get("size") == fingerprint[1]
        and verification.get("sha256") == staged_hash
        and not verification.get("admissionBlockers")
    )


class _KnownAcquisitionProgressExecutor:
    action_code = "resume_known_transition"
    _statuses = {"queued", "downloading", "awaiting_staging", "staging_observed"}

    @staticmethod
    def _current_plan(acquisition_id: int) -> dict[str, Any] | None:
        with local_conn() as conn:
            decision = next((
                item for item in _acquisition_decisions(conn, 500)
                if item.get("subjectKind") == "acquisition"
                and str(item.get("subjectId")) == str(acquisition_id)
            ), None)
            return _build_plan(conn, decision) if decision else None

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if (plan.get("planKind"), plan.get("subjectKind"), plan.get("reasonCode")) != (
            "RECONCILE_ACQUISITION", "acquisition", "ACQUISITION_PROGRESSABLE"
        ):
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "Only a progressable acquisition can be resumed."
            )
        acquisition_id = int(plan["subjectId"])
        acquisition = ebook_acquisition_by_id(acquisition_id)
        current = self._current_plan(acquisition_id)
        if not acquisition or not current:
            raise core.AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED", "The known acquisition is no longer current."
            )
        try:
            items, partial = _queue_payload(BinderyClient())
            inventory = list_staged_ebooks(1000)
        except (AcquisitionSafetyError, StagingSafetyError) as exc:
            raise core.AutomaticExecutionBlocked(
                "DEPENDENCY_UNAVAILABLE", str(exc)
            ) from exc
        queue_id = acquisition.get("queue_id")
        queue, exact_identity, no_competing = _queue_identity(acquisition, items)
        staged = list(inventory.get("items") or [])
        fingerprint = (
            (str(staged[0]["relativePath"]), int(staged[0]["size"]),
             int(staged[0]["modifiedNs"]))
            if len(staged) == 1 else None
        )
        staged_hash = ""
        if fingerprint:
            try:
                _, staged_path = resolve_staged_file(fingerprint[0])
                staged_hash = sha256_file(staged_path)
            except (StagingSafetyError, OSError) as exc:
                raise core.AutomaticExecutionBlocked(
                    "STAGING_IDENTITY_UNPROVEN", str(exc)
                ) from exc
        checks = [
            {"code": "CURRENT_PLAN", "ok": (
                current.get("signature") == plan.get("signature")
                and current.get("evidenceRevision") == plan.get("evidenceRevision")
                and current.get("path") == plan.get("path")
            )},
            {"code": "DURABLE_SUBJECT", "ok": (
                acquisition.get("status") in self._statuses
                and acquisition.get("admission_id") is None
                and not acquisition.get("staged_sha256")
                and acquisition.get("result_id") == plan.get("resultId")
                and acquisition.get("book_id") == plan.get("bookId")
            )},
            {"code": "COMPLETE_QUEUE", "ok": not partial},
            {"code": "EXACT_QUEUE_IDENTITY", "ok": exact_identity},
            {"code": "NO_COMPETING_QUEUE", "ok": no_competing},
            {"code": "QUEUE_HANDOFF_COMPLETE", "ok": (
                _queue_status(queue) in _AWAITING_STAGING_QUEUE_STATUSES
            )},
            {"code": "SINGLE_STAGED_FILE", "ok": (
                not inventory.get("truncated") and fingerprint is not None
                and (not acquisition.get("observed_relative_path")
                     or acquisition["observed_relative_path"] == fingerprint[0])
                and (acquisition.get("status") != "staging_observed"
                     or (acquisition.get("observed_size") == fingerprint[1]
                         and acquisition.get("observed_modified_ns") == fingerprint[2]))
            )},
        ]
        return {
            "ok": all(check["ok"] for check in checks),
            "planSignature": plan["signature"],
            "evidenceRevision": plan["evidenceRevision"],
            "checks": checks,
            "acquisitionId": acquisition_id,
            "queueId": queue_id,
            "queueStatus": _queue_status(queue),
            "fingerprint": fingerprint,
            "stagedSha256": staged_hash,
            "beforeStatus": acquisition["status"],
        }

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        # Re-read immediately before the guarded primitive acquires its own lock.
        fresh = self.revalidate(plan, step)
        core._require_fresh_boundary(plan, fresh)
        if (fresh["queueId"], fresh["fingerprint"], fresh["stagedSha256"], fresh["beforeStatus"]) != (
            boundary["queueId"], boundary["fingerprint"], boundary["stagedSha256"], boundary["beforeStatus"]
        ):
            raise RuntimeError("The queue or staged file changed before progression.")
        response = reconcile_ebook_acquisition(
            int(plan["subjectId"]),
            expected_queue_id=boundary["queueId"],
            expected_staged_fingerprint=tuple(boundary["fingerprint"]),
            expected_status=boundary["beforeStatus"],
        )
        updated = response.get("acquisition") or {}
        _, staged_path = resolve_staged_file(boundary["fingerprint"][0])
        if sha256_file(staged_path) != boundary["stagedSha256"]:
            raise RuntimeError("Staged bytes changed during reconciliation.")
        if not response.get("ok") or updated.get("status") not in {
            "staging_observed", "verified"
        }:
            raise RuntimeError("The known acquisition did not progress safely.")
        if updated["status"] == "staging_observed":
            if (
                updated.get("observed_relative_path") != boundary["fingerprint"][0]
                or updated.get("observed_size") != boundary["fingerprint"][1]
                or updated.get("observed_modified_ns") != boundary["fingerprint"][2]
            ):
                raise RuntimeError("The observed staged identity did not persist.")
        else:
            if updated.get("staged_relative_path") != boundary["fingerprint"][0]:
                raise RuntimeError("Verified staging path differs from the boundary.")
            _, staged_path = resolve_staged_file(updated["staged_relative_path"])
            if updated.get("staged_sha256") != boundary["stagedSha256"]:
                raise RuntimeError("Verified staged bytes changed after reconciliation.")
        return {
            "acquisitionId": int(plan["subjectId"]),
            "status": updated["status"],
            "queueId": updated.get("queue_id"),
            "stagedRelativePath": boundary["fingerprint"][0],
            "stagedSha256": updated.get("staged_sha256"),
            "externalMutationPerformed": False,
            "admissionAttempted": False,
        }

    def reconcile_uncertain(
        self, plan: dict[str, Any], step: dict[str, Any], existing: dict[str, Any]
    ) -> dict[str, Any]:
        boundary = existing.get("boundary") or {}
        acquisition = ebook_acquisition_by_id(int(plan["subjectId"])) or {}
        fingerprint = boundary.get("fingerprint")
        before_status = str(boundary.get("beforeStatus") or "")
        if (
            plan.get("planKind") != "RECONCILE_ACQUISITION"
            or plan.get("subjectKind") != "acquisition"
            or plan.get("reasonCode") != "ACQUISITION_PROGRESSABLE"
            or acquisition.get("result_id") != plan.get("resultId")
            or acquisition.get("book_id") != plan.get("bookId")
            or acquisition.get("admission_id") is not None
            or not fingerprint
            or acquisition.get("queue_id") != boundary.get("queueId")
        ):
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", "Interrupted queue identity is unproven."
            )
        try:
            items, partial = _queue_payload(BinderyClient())
            queue, exact_identity, no_competing = _queue_identity(acquisition, items)
            if (
                partial or not exact_identity or not no_competing
                or _queue_status(queue) not in _AWAITING_STAGING_QUEUE_STATUSES
            ):
                raise ValueError("The current queue handoff is unproven.")
            inventory = list_staged_ebooks(1000)
            staged = inventory.get("items") or []
            actual = (
                str(staged[0]["relativePath"]), int(staged[0]["size"]),
                int(staged[0]["modifiedNs"])
            ) if len(staged) == 1 and not inventory.get("truncated") else None
            if actual != tuple(fingerprint):
                raise ValueError("Staged fingerprint changed after interruption.")
            _, path = resolve_staged_file(fingerprint[0])
            if sha256_file(path) != boundary.get("stagedSha256"):
                raise ValueError("Staged bytes changed after interruption.")
            if (acquisition.get("status") == "staging_observed"
                    and before_status in {"queued", "downloading", "awaiting_staging"}):
                proven = (
                    acquisition.get("observed_relative_path") == fingerprint[0]
                    and acquisition.get("observed_size") == fingerprint[1]
                    and acquisition.get("observed_modified_ns") == fingerprint[2]
                )
            elif acquisition.get("status") == "verified" and before_status in self._statuses:
                proven = (
                    acquisition.get("staged_relative_path") == fingerprint[0]
                    and bool(acquisition.get("staged_sha256"))
                    and boundary["stagedSha256"] == acquisition["staged_sha256"]
                    and _verified_snapshot_matches(
                        acquisition, int(plan["bookId"]), tuple(fingerprint),
                        str(boundary["stagedSha256"]),
                    )
                )
            else:
                proven = False
        except (AcquisitionSafetyError, StagingSafetyError, OSError, ValueError) as exc:
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME", str(exc)
            ) from exc
        if not proven:
            raise core.AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted progression has no independently proven result."
            )
        return {
            "acquisitionId": int(plan["subjectId"]),
            "status": acquisition["status"],
            "queueId": acquisition["queue_id"],
            "stagedRelativePath": fingerprint[0],
            "stagedSha256": acquisition.get("staged_sha256"),
            "externalMutationPerformed": False,
            "reconciledAfterRestart": True,
        }


EXECUTOR = _KnownAcquisitionProgressExecutor()


def register_executor() -> None:
    core.register_automatic_executor(EXECUTOR.action_code, EXECUTOR)


def run_acquisition_progress_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = int(plan["id"])
    refreshed = core.recovery_plan_by_id(plan_id) or plan
    steps = refreshed.get("steps") or []
    index = int(refreshed.get("currentStep") or 0)
    code = str(steps[index].get("code") or "") if index < len(steps) else ""
    if code == "reobserve_acquisition":
        try:
            boundary = EXECUTOR.revalidate(refreshed, steps[index])
        except core.AutomaticExecutionBlocked as exc:
            blocked = core.block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False, "state": "blocked", "plan": blocked,
                "externalMutationAttempted": False,
                "reasonCode": exc.reason_code, "message": str(exc),
            }
        if not boundary["ok"]:
            failed = [item["code"] for item in boundary["checks"] if not item["ok"]]
            if set(failed) <= {"QUEUE_HANDOFF_COMPLETE", "SINGLE_STAGED_FILE"} and (
                "SINGLE_STAGED_FILE" not in failed
                or not boundary.get("fingerprint")
            ) and boundary.get("beforeStatus") != "staging_observed" and (
                boundary.get("queueStatus") in {"queued", "downloading"}
                or (boundary.get("queueStatus") in _AWAITING_STAGING_QUEUE_STATUSES
                    and not boundary.get("fingerprint"))
            ):
                # A known queue may still be downloading or awaiting the staging file.
                return {
                    "ok": True, "state": "waiting", "plan": refreshed,
                    "externalMutationAttempted": False,
                    "message": "Waiting for the exact queue handoff and one staged file.",
                }
            blocked = core.block_recovery_plan(plan_id, ", ".join(failed))
            return {
                "ok": False, "state": "blocked", "plan": blocked,
                "externalMutationAttempted": False,
                "reasonCode": "BOUNDARY_INVARIANT_FAILED",
                "message": ", ".join(failed),
            }
        core._require_fresh_boundary(refreshed, boundary)
        refreshed = core.record_recovery_step_success(plan_id, index)
    try:
        result = core.attempt_automatic_step(plan_id)
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(plan_id, str(exc))
        return {
            "ok": False, "state": "blocked", "plan": blocked,
            "externalMutationAttempted": False,
            "reasonCode": exc.reason_code, "message": str(exc),
        }
    return {
        **result,
        "state": "reconciled" if result.get("reconciled") else
                 "replayed" if result.get("replayed") else "executed",
        "externalMutationAttempted": False,
    }
