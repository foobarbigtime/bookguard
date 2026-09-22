from __future__ import annotations

import json
from typing import Any, Protocol

from .acquisition import (
    AcquisitionSafetyError,
    _result_and_book,
    _search_candidate,
    acquisition_readiness,
    reconcile_ebook_acquisition,
    retry_failed_ebook_acquisition,
)
from .bindery_client import BinderyClient
from .config import AUTOMATIC_ACTION_CODES, load_automation_settings
from .db import ebook_acquisition_by_id, local_conn, result_by_id, utc_now
from .observe import _acquisition_decisions
from .recovery_classifier import classify_acquisition_failure
from .recovery_planner import (
    _build_plan,
    block_recovery_plan,
    promote_due_recovery_retries,
    recovery_plan_by_id,
    recovery_plan_snapshot,
    record_recovery_step_success,
    schedule_recovery_retry,
)


class AutomaticExecutionBlocked(RuntimeError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class AutomaticExecutor(Protocol):
    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]: ...
    def execute(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any] | None: ...


_EXECUTORS: dict[str, AutomaticExecutor] = {}


class _TransientAcquisitionRetryExecutor:
    action_code = "retry_grab_once"

    def revalidate(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
    ) -> dict[str, Any]:
        if str(plan.get("planKind") or "") != "RETRY_ACQUISITION_TRANSIENT":
            raise AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH",
                "retry_grab_once is valid only for RETRY_ACQUISITION_TRANSIENT.",
            )
        if str(plan.get("subjectKind") or "") != "acquisition":
            raise AutomaticExecutionBlocked(
                "SUBJECT_KIND_MISMATCH",
                "Transient acquisition retry requires an acquisition subject.",
            )

        acquisition_id = int(plan["subjectId"])
        acquisition = ebook_acquisition_by_id(acquisition_id)
        if not acquisition:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The planned acquisition no longer exists.",
            )

        recovery = classify_acquisition_failure(
            str(acquisition.get("status") or ""),
            str(acquisition.get("error") or ""),
        )
        if (
            recovery is None
            or recovery.reason_code != "ACQUISITION_TRANSIENT_BINDERY_FAILURE"
            or not recovery.retry_same_operation
        ):
            raise AutomaticExecutionBlocked(
                "RECOVERY_CLASSIFICATION_CHANGED",
                "The acquisition is no longer classified as a retryable transient failure.",
            )

        with local_conn() as conn:
            decisions = _acquisition_decisions(conn, 500)
            current_decision = next(
                (
                    item
                    for item in decisions
                    if str(item.get("subjectKind") or "") == "acquisition"
                    and str(item.get("subjectId") or "") == str(acquisition_id)
                ),
                None,
            )
            current_plan = _build_plan(conn, current_decision) if current_decision else None

        if not current_plan:
            raise AutomaticExecutionBlocked(
                "DECISION_NO_LONGER_AUTHORIZED",
                "The current decision policy no longer authorizes a recovery plan.",
            )

        client = BinderyClient()
        result = result_by_id(int(acquisition["result_id"]))
        if not result:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The acquisition's scan result no longer exists.",
            )

        readiness = acquisition_readiness(client)
        if not readiness["ready"]:
            raise AutomaticExecutionBlocked(
                "ACQUISITION_READINESS_FAILED",
                "Fresh acquisition readiness failed: " + ", ".join(readiness["blockers"]),
            )

        _, expected_title, expected_author = _result_and_book(result, client)
        candidate = _search_candidate(
            client,
            int(acquisition["book_id"]),
            str(acquisition.get("candidate_guid") or ""),
            expected_title,
            expected_author,
        )
        candidate_same = (
            str(candidate.get("title") or "").strip()
            == str(acquisition.get("candidate_title") or "").strip()
            and (
                not str(acquisition.get("candidate_protocol") or "").strip()
                or str(candidate.get("protocol") or "").strip().casefold()
                == str(acquisition.get("candidate_protocol") or "").strip().casefold()
            )
        )

        checks = [
            {
                "code": "SUBJECT_IDENTITY_UNCHANGED",
                "ok": (
                    str(current_plan.get("subjectId") or "") == str(plan.get("subjectId") or "")
                    and current_plan.get("resultId") == plan.get("resultId")
                    and current_plan.get("bookId") == plan.get("bookId")
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
                "ok": str(acquisition.get("status") or "").casefold() == "failed",
            },
            {
                "code": "RECOVERY_CLASSIFICATION_UNCHANGED",
                "ok": recovery.reason_code == str(plan.get("reasonCode") or ""),
            },
            {"code": "ACQUISITION_READINESS_CURRENT", "ok": bool(readiness["ready"])},
            {"code": "CANDIDATE_IDENTITY_UNCHANGED", "ok": candidate_same},
        ]

        return {
            "ok": all(check["ok"] for check in checks),
            "planSignature": str(plan["signature"]),
            "evidenceRevision": str(plan["evidenceRevision"]),
            "checks": checks,
            "acquisitionId": acquisition_id,
            "candidateGuid": str(acquisition.get("candidate_guid") or ""),
        }

    def reconcile_uncertain(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        """Adopt a proven remote grab after an interrupted running execution.

        A running journal row means the process may have died after Bindery
        accepted the grab. Never send the grab again. Reconcile only when the
        current Bindery queue contains exactly one item matching the durable
        book, candidate title, and protocol.
        """
        acquisition_id = int(plan["subjectId"])
        acquisition = ebook_acquisition_by_id(acquisition_id)
        if not acquisition:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The interrupted acquisition no longer exists.",
            )
        if str(acquisition.get("status") or "").casefold() != "grab_requested":
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted grab cannot be adopted because the durable acquisition "
                "is not in grab_requested state.",
            )

        client = BinderyClient()
        try:
            payload = client.list_queue()
        except Exception as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Interrupted grab outcome could not be checked safely: {exc}",
            ) from exc

        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
            partial = False
            structurally_complete = len(items) == len(payload)
        elif isinstance(payload, dict):
            raw_items = payload.get("items")
            structurally_complete = isinstance(raw_items, list)
            items = (
                [item for item in raw_items if isinstance(item, dict)]
                if structurally_complete
                else []
            )
            structurally_complete = (
                structurally_complete and len(items) == len(raw_items)
            )
            partial = bool(payload.get("partial", False))
        else:
            items = []
            partial = False
            structurally_complete = False

        if not structurally_complete or partial:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted grab outcome cannot be adopted from a partial or invalid "
                "Bindery queue response.",
            )

        expected_book = str(acquisition.get("book_id") or "")
        expected_title = str(acquisition.get("candidate_title") or "").strip().casefold()
        expected_protocol = (
            str(acquisition.get("candidate_protocol") or "").strip().casefold()
        )
        matches = []
        for item in items:
            item_protocol = str(item.get("protocol") or "").strip().casefold()
            if (
                str(item.get("bookId") or "") == expected_book
                and str(item.get("title") or "").strip().casefold() == expected_title
                and (not expected_protocol or item_protocol == expected_protocol)
            ):
                matches.append(item)

        if len(matches) != 1 or matches[0].get("id") is None:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted grab was not proven by exactly one current Bindery queue "
                "item with the durable book, candidate title, and protocol.",
            )

        expected_queue_id = matches[0].get("id")
        try:
            reconciled = reconcile_ebook_acquisition(acquisition_id, client)
        except AcquisitionSafetyError as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Interrupted grab queue proof could not be reconciled safely: {exc}",
            ) from exc

        updated = reconciled.get("acquisition") or {}
        adopted_status = str(updated.get("status") or "")
        adopted_queue_id = updated.get("queue_id")
        if (
            adopted_status
            not in {
                "queued",
                "downloading",
                "awaiting_staging",
                "staging_observed",
                "verified",
            }
            or str(adopted_queue_id or "") != str(expected_queue_id)
        ):
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted grab queue proof did not reconcile to the same durable "
                "queue item.",
            )

        return {
            "acquisitionId": acquisition_id,
            "status": adopted_status,
            "queueId": adopted_queue_id,
            "reconciledAfterRestart": True,
            "proof": {
                "bookId": int(acquisition["book_id"]),
                "candidateTitle": str(acquisition.get("candidate_title") or ""),
                "candidateProtocol": str(acquisition.get("candidate_protocol") or ""),
            },
        }

    def execute(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = retry_failed_ebook_acquisition(int(plan["subjectId"]))
        except AcquisitionSafetyError as exc:
            raise RuntimeError(str(exc)) from exc
        acquisition = result.get("acquisition") or {}
        return {
            "acquisitionId": int(plan["subjectId"]),
            "status": str(acquisition.get("status") or ""),
            "queueId": acquisition.get("queue_id"),
        }


_EXECUTORS["retry_grab_once"] = _TransientAcquisitionRetryExecutor()


def register_automatic_executor(action_code: str, executor: AutomaticExecutor) -> None:
    code = str(action_code or "").strip()
    if code not in AUTOMATIC_ACTION_CODES:
        raise ValueError(f"Unsupported automatic action code: {code}")
    _EXECUTORS[code] = executor


def execution_policy_snapshot() -> dict[str, Any]:
    configured = load_automation_settings()
    allowlisted = sorted(configured.automatic_action_allowlist)
    registered = sorted(_EXECUTORS)
    executable = sorted(set(allowlisted) & set(registered))
    return {
        "mode": configured.automation_mode,
        "supportedActionCodes": sorted(AUTOMATIC_ACTION_CODES),
        "allowlistedActionCodes": allowlisted,
        "registeredExecutorCodes": registered,
        "executableActionCodes": executable,
        "automaticModeActive": configured.automation_mode == "automatic",
        "liveMutationReady": (
            configured.automation_mode == "automatic" and bool(executable)
        ),
        "manualMutationEndpointsAllowed": configured.automation_mode == "manual",
    }


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone() is not None


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _decode(row) -> dict[str, Any]:
    item = dict(row)
    return {
        "id": int(item["id"]),
        "planId": int(item["plan_id"]),
        "planSignature": str(item["plan_signature"]),
        "actionCode": str(item["action_code"]),
        "stepIndex": int(item["step_index"]),
        "state": str(item["state"]),
        "evidenceRevision": str(item["evidence_revision"]),
        "boundary": _json_object(item.get("boundary_json")),
        "externalResult": _json_object(item.get("external_result_json")),
        "error": str(item.get("error") or ""),
        "createdAt": str(item["created_at"]),
        "updatedAt": str(item["updated_at"]),
        "completedAt": item.get("completed_at"),
        "attemptCount": int(item.get("attempt_count") or 0),
    }


def automatic_execution_history(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 1000))
    with local_conn() as conn:
        if not _table_exists(conn, "automatic_executions"):
            rows = []
        else:
            rows = conn.execute(
                "SELECT * FROM automatic_executions ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    items = [_decode(row) for row in rows]
    return {"count": len(items), "items": items}


def _existing(plan_signature: str, action_code: str, step_index: int) -> dict[str, Any] | None:
    with local_conn() as conn:
        if not _table_exists(conn, "automatic_executions"):
            return None
        row = conn.execute(
            """
            SELECT * FROM automatic_executions
            WHERE plan_signature=? AND action_code=? AND step_index=?
            LIMIT 1
            """,
            (plan_signature, action_code, int(step_index)),
        ).fetchone()
    return _decode(row) if row else None


def _record(
    plan: dict[str, Any],
    action_code: str,
    step_index: int,
    state: str,
    *,
    boundary: dict[str, Any] | None = None,
    external_result: dict[str, Any] | None = None,
    error: str = "",
    increment_attempt: bool = False,
) -> dict[str, Any]:
    now = utc_now()
    completed_at = now if state in {"succeeded", "failed", "blocked"} else None
    inc = 1 if increment_attempt else 0
    with local_conn() as conn:
        if not _table_exists(conn, "automatic_executions"):
            raise RuntimeError("Automatic execution journal is unavailable.")
        conn.execute(
            """
            INSERT INTO automatic_executions(
                plan_id, plan_signature, action_code, step_index, state,
                evidence_revision, boundary_json, external_result_json, error,
                created_at, updated_at, completed_at, attempt_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_signature, action_code, step_index) DO UPDATE SET
                plan_id=excluded.plan_id,
                state=excluded.state,
                evidence_revision=excluded.evidence_revision,
                boundary_json=excluded.boundary_json,
                external_result_json=excluded.external_result_json,
                error=excluded.error,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at,
                attempt_count=automatic_executions.attempt_count + ?
            """,
            (
                int(plan["id"]), str(plan["signature"]), action_code, int(step_index),
                state, str(plan["evidenceRevision"]),
                json.dumps(boundary or {}, sort_keys=True),
                json.dumps(external_result or {}, sort_keys=True),
                str(error or ""), now, now, completed_at, inc, inc,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT * FROM automatic_executions
            WHERE plan_signature=? AND action_code=? AND step_index=?
            LIMIT 1
            """,
            (str(plan["signature"]), action_code, int(step_index)),
        ).fetchone()
    return _decode(row)


def _require_fresh_boundary(plan: dict[str, Any], boundary: dict[str, Any]) -> None:
    if not isinstance(boundary, dict) or boundary.get("ok") is not True:
        raise AutomaticExecutionBlocked(
            "BOUNDARY_REVALIDATION_FAILED",
            "Fresh mutation-boundary revalidation did not authorize this step.",
        )
    if str(boundary.get("planSignature") or "") != str(plan["signature"]):
        raise AutomaticExecutionBlocked(
            "PLAN_SIGNATURE_CHANGED",
            "Boundary evidence does not match the current recovery plan.",
        )
    if str(boundary.get("evidenceRevision") or "") != str(plan["evidenceRevision"]):
        raise AutomaticExecutionBlocked(
            "EVIDENCE_REVISION_CHANGED",
            "Boundary evidence revision changed; the plan must be rebuilt.",
        )
    checks = boundary.get("checks")
    if not isinstance(checks, list) or not checks:
        raise AutomaticExecutionBlocked(
            "BOUNDARY_CHECKS_MISSING",
            "Automatic mutation requires explicit fresh boundary checks.",
        )
    failed = [
        str(check.get("code") or "UNKNOWN")
        for check in checks
        if not isinstance(check, dict) or check.get("ok") is not True
    ]
    if failed:
        raise AutomaticExecutionBlocked(
            "BOUNDARY_INVARIANT_FAILED",
            "Fresh mutation-boundary invariant failed: " + ", ".join(failed),
        )


def run_automatic_cycle(limit: int = 100) -> dict[str, Any]:
    """Advance at most one E4 work item and perform at most one external mutation.

    The first live allowlisted executor is RETRY_ACQUISITION_TRANSIENT /
    retry_grab_once. All other recovery plan kinds remain untouched.
    """
    configured = load_automation_settings()
    if configured.automation_mode != "automatic":
        raise AutomaticExecutionBlocked(
            "AUTOMATIC_MODE_INACTIVE",
            "BOOKGUARD_AUTOMATION_MODE must be automatic.",
        )

    promoted = promote_due_recovery_retries()
    snapshot = recovery_plan_snapshot(limit)
    candidates = [
        item
        for item in snapshot["items"]
        if item.get("planKind") == "RETRY_ACQUISITION_TRANSIENT"
        and item.get("state") in {"planned", "ready", "retry_wait"}
    ]
    candidates.sort(key=lambda item: int(item["id"]))

    if not candidates:
        return {
            "ok": True,
            "state": "idle",
            "promotedPlanIds": [int(item["id"]) for item in promoted],
            "externalMutationAttempted": False,
            "message": "No transient acquisition retry plan is ready for automatic work.",
        }

    plan = candidates[0]
    plan_id = int(plan["id"])

    if plan["state"] == "planned":
        scheduled = schedule_recovery_retry(
            plan_id,
            "Automatic Mode scheduled the bounded transient acquisition retry.",
        )
        return {
            "ok": True,
            "state": "retry_wait",
            "plan": scheduled,
            "externalMutationAttempted": False,
            "message": "The first bounded retry interval was scheduled; no external work ran.",
        }

    if plan["state"] == "retry_wait":
        return {
            "ok": True,
            "state": "retry_wait",
            "plan": plan,
            "externalMutationAttempted": False,
            "message": "The bounded retry interval has not elapsed.",
        }

    steps = list(plan.get("steps") or [])
    current_step = int(plan.get("currentStep") or 0)
    current_code = (
        str((steps[current_step] or {}).get("code") or "")
        if 0 <= current_step < len(steps)
        else ""
    )

    if current_code == "wait_bounded_backoff":
        plan = record_recovery_step_success(plan_id, current_step)
        steps = list(plan.get("steps") or [])
        current_step = int(plan.get("currentStep") or 0)
        current_code = (
            str((steps[current_step] or {}).get("code") or "")
            if 0 <= current_step < len(steps)
            else ""
        )

    if current_code == "revalidate_acquisition_readiness":
        executor = _EXECUTORS.get("retry_grab_once")
        if executor is None:
            raise AutomaticExecutionBlocked(
                "EXECUTOR_NOT_REGISTERED",
                "Transient acquisition retry executor is not registered.",
            )
        try:
            boundary = executor.revalidate(plan, steps[current_step])
            _require_fresh_boundary(plan, boundary)
        except AutomaticExecutionBlocked as exc:
            retryable_boundary_failures = {
                "ACQUISITION_READINESS_FAILED",
                "BOUNDARY_REVALIDATION_ERROR",
            }
            if exc.reason_code in retryable_boundary_failures:
                scheduled = schedule_recovery_retry(plan_id, str(exc))
                return {
                    "ok": False,
                    "state": "retry_wait",
                    "plan": scheduled,
                    "externalMutationAttempted": False,
                    "reasonCode": exc.reason_code,
                    "message": str(exc),
                }
            blocked = block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False,
                "state": "blocked",
                "plan": blocked,
                "externalMutationAttempted": False,
                "reasonCode": exc.reason_code,
                "message": str(exc),
            }
        plan = record_recovery_step_success(plan_id, current_step)

    refreshed = recovery_plan_by_id(plan_id) or plan
    refreshed_steps = list(refreshed.get("steps") or [])
    refreshed_index = int(refreshed.get("currentStep") or 0)
    refreshed_code = (
        str((refreshed_steps[refreshed_index] or {}).get("code") or "")
        if 0 <= refreshed_index < len(refreshed_steps)
        else ""
    )
    if refreshed_code != "retry_grab_once":
        return {
            "ok": True,
            "state": "paused",
            "plan": refreshed,
            "externalMutationAttempted": False,
            "message": (
                "The transient grab retry step is complete. The next recovery step "
                "is not enabled by this E4 slice."
            ),
        }

    try:
        result = attempt_automatic_step(plan_id)
    except AutomaticExecutionBlocked as exc:
        if exc.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME":
            blocked = block_recovery_plan(
                plan_id,
                "Interrupted external mutation outcome could not be proven safely: "
                + str(exc),
            )
            return {
                "ok": False,
                "state": "blocked",
                "plan": blocked,
                "externalMutationAttempted": False,
                "reasonCode": exc.reason_code,
                "message": str(exc),
            }
        if exc.reason_code != "EXECUTION_FAILED":
            raise

        acquisition = ebook_acquisition_by_id(int(refreshed["subjectId"]))
        recovery = (
            classify_acquisition_failure(
                str((acquisition or {}).get("status") or ""),
                str((acquisition or {}).get("error") or ""),
            )
            if acquisition
            else None
        )
        if (
            recovery is not None
            and recovery.reason_code == "ACQUISITION_TRANSIENT_BINDERY_FAILURE"
            and recovery.retry_same_operation
        ):
            scheduled = schedule_recovery_retry(plan_id, str(exc))
            return {
                "ok": False,
                "state": str(scheduled.get("state") or "retry_wait"),
                "plan": scheduled,
                "externalMutationAttempted": True,
                "reasonCode": exc.reason_code,
                "message": (
                    "The live grab retry failed transiently and was returned to the "
                    "persisted bounded-backoff schedule."
                ),
            }

        blocked = block_recovery_plan(
            plan_id,
            "The live grab retry failed and the current failure is no longer "
            "classified as same-operation transient retry: " + str(exc),
        )
        return {
            "ok": False,
            "state": "blocked",
            "plan": blocked,
            "externalMutationAttempted": True,
            "reasonCode": "RECOVERY_CLASSIFICATION_CHANGED",
            "message": str(exc),
        }

    return {
        **result,
        "state": "executed",
        "externalMutationAttempted": True,
    }


def attempt_automatic_step(plan_id: int) -> dict[str, Any]:
    configured = load_automation_settings()
    if configured.automation_mode != "automatic":
        raise AutomaticExecutionBlocked(
            "AUTOMATIC_MODE_INACTIVE",
            "BOOKGUARD_AUTOMATION_MODE must be automatic.",
        )

    plan = recovery_plan_by_id(int(plan_id))
    if not plan:
        raise AutomaticExecutionBlocked("PLAN_NOT_FOUND", "Recovery plan not found.")
    if str(plan.get("state") or "") not in {"planned", "ready"}:
        raise AutomaticExecutionBlocked(
            "PLAN_NOT_READY",
            f"Recovery plan state '{plan.get('state')}' is not executable.",
        )

    step_index = int(plan.get("currentStep") or 0)
    steps = plan.get("steps") or []
    if step_index >= len(steps):
        raise AutomaticExecutionBlocked("STEP_NOT_FOUND", "Recovery plan has no current step.")
    step = steps[step_index]
    action_code = str(step.get("code") or "")
    if step.get("externalMutation") is not True:
        raise AutomaticExecutionBlocked(
            "READ_ONLY_STEP_PENDING",
            "The current recovery step is read-only.",
        )
    if action_code not in AUTOMATIC_ACTION_CODES:
        raise AutomaticExecutionBlocked("UNSUPPORTED_ACTION", action_code)
    if action_code not in configured.automatic_action_allowlist:
        raise AutomaticExecutionBlocked(
            "ACTION_NOT_ALLOWLISTED",
            f"Recovery step '{action_code}' is not allowlisted.",
        )

    executor = _EXECUTORS.get(action_code)
    if executor is None:
        _record(
            plan,
            action_code,
            step_index,
            "blocked",
            error="No live executor is registered for this action code.",
        )
        raise AutomaticExecutionBlocked(
            "EXECUTOR_NOT_REGISTERED",
            f"'{action_code}' has no registered live executor; nothing was mutated.",
        )

    existing = _existing(str(plan["signature"]), action_code, step_index)
    if existing and existing["state"] == "succeeded":
        return {
            "ok": True,
            "replayed": True,
            "execution": existing,
            "plan": record_recovery_step_success(int(plan_id), step_index),
        }

    if existing and existing["state"] == "running":
        reconciler = getattr(executor, "reconcile_uncertain", None)
        if not callable(reconciler):
            message = (
                "A prior external mutation attempt is still recorded as running; "
                "automatic replay is forbidden until its outcome is proven."
            )
            _record(
                plan,
                action_code,
                step_index,
                "blocked",
                boundary=existing.get("boundary") or {},
                error=message,
            )
            raise AutomaticExecutionBlocked("UNCERTAIN_EXTERNAL_OUTCOME", message)
        try:
            result = reconciler(plan, step, existing) or {}
        except Exception as exc:
            message = str(exc)
            _record(
                plan,
                action_code,
                step_index,
                "blocked",
                boundary=existing.get("boundary") or {},
                error=message,
            )
            if isinstance(exc, AutomaticExecutionBlocked):
                raise
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                message,
            ) from exc

        execution = _record(
            plan,
            action_code,
            step_index,
            "succeeded",
            boundary=existing.get("boundary") or {},
            external_result=result,
        )
        updated_plan = record_recovery_step_success(int(plan_id), step_index)
        return {
            "ok": True,
            "replayed": True,
            "reconciled": True,
            "execution": execution,
            "plan": updated_plan,
        }

    try:
        boundary = executor.revalidate(plan, step)
        _require_fresh_boundary(plan, boundary)
    except Exception as exc:
        message = str(exc)
        _record(plan, action_code, step_index, "blocked", error=message)
        if isinstance(exc, AutomaticExecutionBlocked):
            raise
        raise AutomaticExecutionBlocked("BOUNDARY_REVALIDATION_ERROR", message) from exc

    _record(
        plan,
        action_code,
        step_index,
        "running",
        boundary=boundary,
        increment_attempt=True,
    )
    try:
        result = executor.execute(plan, step, boundary) or {}
    except Exception as exc:
        _record(plan, action_code, step_index, "failed", boundary=boundary, error=str(exc))
        raise AutomaticExecutionBlocked("EXECUTION_FAILED", str(exc)) from exc

    execution = _record(
        plan,
        action_code,
        step_index,
        "succeeded",
        boundary=boundary,
        external_result=result,
    )
    updated_plan = record_recovery_step_success(int(plan_id), step_index)
    return {"ok": True, "replayed": False, "execution": execution, "plan": updated_plan}
