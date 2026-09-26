from __future__ import annotations

import json
from typing import Any, Protocol

from .automatic_admission import _KnownAdmissionReconcileExecutor
from .automatic_contracts import AutomaticExecutionBlocked
from .automatic_quarantine import _UnsafeMediaQuarantineExecutor
from .automatic_retry import _TransientAcquisitionRetryExecutor
from .config import AUTOMATIC_ACTION_CODES, load_automation_settings
from .db import (
    ebook_acquisition_by_id,
    local_conn,
    utc_now,
)
from .observe import _acquisition_decisions
from .recovery_classifier import classify_acquisition_failure
from .recovery_planner import (
    block_recovery_plan,
    promote_due_recovery_retries,
    record_recovery_plans,
    recovery_plan_by_id,
    recovery_plan_snapshot,
    record_recovery_step_success,
    schedule_recovery_retry,
)


class AutomaticExecutor(Protocol):
    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]: ...
    def execute(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any] | None: ...


_EXECUTORS: dict[str, AutomaticExecutor] = {}


_EXECUTORS["retry_grab_once"] = _TransientAcquisitionRetryExecutor()
_EXECUTORS["quarantine_exact_media"] = _UnsafeMediaQuarantineExecutor()
_EXECUTORS["reconcile_known_admission"] = _KnownAdmissionReconcileExecutor()


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
                boundary_json=CASE
                    WHEN excluded.boundary_json='{}'
                    THEN automatic_executions.boundary_json
                    ELSE excluded.boundary_json
                END,
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


def _handoff_completed_retry(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Replace a proven completed grab plan with current read-only E3 authority."""
    steps = list(plan.get("steps") or [])
    index = int(plan.get("currentStep") or 0)
    if (
        plan.get("planKind") != "RETRY_ACQUISITION_TRANSIENT"
        or plan.get("subjectKind") != "acquisition"
        or index != 3
        or len(steps) <= index
        or steps[index].get("code") != "reconcile_after_retry"
        or steps[index - 1].get("code") != "retry_grab_once"
    ):
        return None
    execution = _existing(str(plan.get("signature") or ""), "retry_grab_once", index - 1)
    if not execution or execution.get("state") != "succeeded":
        return None
    acquisition = ebook_acquisition_by_id(int(plan["subjectId"]))
    if (
        not acquisition
        or acquisition.get("status") not in {
            "queued", "downloading", "awaiting_staging", "staging_observed"
        }
        or acquisition.get("queue_id") is None
        or acquisition.get("admission_id") is not None
        or acquisition.get("result_id") != plan.get("resultId")
        or acquisition.get("book_id") != plan.get("bookId")
    ):
        return None
    prior_result = execution.get("externalResult") or {}
    if (
        str(prior_result.get("acquisitionId") or "") != str(plan["subjectId"])
        or str(prior_result.get("queueId") or "")
        != str(acquisition["queue_id"])
    ):
        return None

    with local_conn() as conn:
        current = next((
            item for item in _acquisition_decisions(conn, 500)
            if item.get("subjectKind") == "acquisition"
            and str(item.get("subjectId")) == str(plan["subjectId"])
        ), None)
        if (
            not current
            or current.get("decision") != "would_reconcile_acquisition"
            or current.get("reasonCode") != "ACQUISITION_PROGRESSABLE"
            or current.get("resultId") != plan.get("resultId")
            or current.get("bookId") != plan.get("bookId")
            or str((current.get("evidence") or {}).get("queueId") or "")
            != str(acquisition["queue_id"])
        ):
            return None
        new_plan = record_recovery_plans(conn, [current])[0]
        conn.commit()
    if new_plan.get("planKind") != "RECONCILE_ACQUISITION":
        raise RuntimeError("Retry handoff did not create the expected E3 plan.")
    return {
        "ok": True,
        "state": "handed_off",
        "plan": new_plan,
        "previousPlanId": int(plan["id"]),
        "externalMutationAttempted": False,
        "message": (
            "The proven retry is now governed by a new known-acquisition staging "
            "plan. No grab or admission was attempted."
        ),
    }


def _run_unsafe_quarantine_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    """Advance one exact unsafe-media quarantine plan and no other mutation class."""
    plan_id = int(plan["id"])
    executor = _EXECUTORS.get("quarantine_exact_media")
    if executor is None:
        raise AutomaticExecutionBlocked(
            "EXECUTOR_NOT_REGISTERED",
            "Unsafe-media quarantine executor is not registered.",
        )

    for read_only_code in (
        "revalidate_unsafe_verdict",
        "capture_exact_source_identity",
    ):
        refreshed = recovery_plan_by_id(plan_id) or plan
        steps = list(refreshed.get("steps") or [])
        index = int(refreshed.get("currentStep") or 0)
        code = (
            str((steps[index] or {}).get("code") or "")
            if 0 <= index < len(steps)
            else ""
        )
        if code != read_only_code:
            continue

        try:
            boundary = executor.revalidate(refreshed, steps[index])
            _require_fresh_boundary(refreshed, boundary)
        except AutomaticExecutionBlocked as exc:
            blocked = block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False,
                "state": "blocked",
                "plan": blocked,
                "externalMutationAttempted": False,
                "reasonCode": exc.reason_code,
                "message": str(exc),
            }

        plan = record_recovery_step_success(plan_id, index)

    refreshed = recovery_plan_by_id(plan_id) or plan
    steps = list(refreshed.get("steps") or [])
    index = int(refreshed.get("currentStep") or 0)
    code = (
        str((steps[index] or {}).get("code") or "")
        if 0 <= index < len(steps)
        else ""
    )

    if code != "quarantine_exact_media":
        return {
            "ok": True,
            "state": "paused",
            "plan": refreshed,
            "externalMutationAttempted": False,
            "message": (
                "The exact unsafe-media quarantine step is complete or not current. "
                "Later recovery steps remain disabled by this E4 slice."
            ),
        }

    try:
        result = attempt_automatic_step(plan_id)
    except AutomaticExecutionBlocked as exc:
        if exc.reason_code in {
            "ACTION_NOT_ALLOWLISTED",
            "EXECUTOR_NOT_REGISTERED",
        }:
            raise
        blocked = block_recovery_plan(
            plan_id,
            "Unsafe-media quarantine stopped safely: " + str(exc),
        )
        return {
            "ok": False,
            "state": "blocked",
            "plan": blocked,
            "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED",
            "reasonCode": exc.reason_code,
            "message": str(exc),
        }

    replayed = bool(result.get("replayed"))
    reconciled = bool(result.get("reconciled"))
    return {
        **result,
        "state": (
            "reconciled"
            if reconciled
            else "replayed"
            if replayed
            else "executed"
        ),
        "externalMutationAttempted": not replayed,
    }


def _run_admission_reconcile_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    """Advance one admission-subject registration reconciliation plan."""
    plan_id = int(plan["id"])
    executor = _EXECUTORS.get("reconcile_known_admission")
    if executor is None:
        raise AutomaticExecutionBlocked(
            "EXECUTOR_NOT_REGISTERED",
            "Known-admission reconciliation executor is not registered.",
        )

    refreshed = recovery_plan_by_id(plan_id) or plan
    steps = list(refreshed.get("steps") or [])
    index = int(refreshed.get("currentStep") or 0)
    code = (
        str((steps[index] or {}).get("code") or "")
        if 0 <= index < len(steps)
        else ""
    )

    if code == "reobserve_registration":
        try:
            boundary = executor.revalidate(refreshed, steps[index])
            _require_fresh_boundary(refreshed, boundary)
        except AutomaticExecutionBlocked as exc:
            blocked = block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False,
                "state": "blocked",
                "plan": blocked,
                "externalMutationAttempted": False,
                "reasonCode": exc.reason_code,
                "message": str(exc),
            }
        refreshed = record_recovery_step_success(plan_id, index)

    refreshed = recovery_plan_by_id(plan_id) or refreshed
    steps = list(refreshed.get("steps") or [])
    index = int(refreshed.get("currentStep") or 0)
    code = (
        str((steps[index] or {}).get("code") or "")
        if 0 <= index < len(steps)
        else ""
    )

    if code != "reconcile_known_admission":
        return {
            "ok": True,
            "state": "paused",
            "plan": refreshed,
            "externalMutationAttempted": False,
            "message": (
                "The known admission reconciliation step is complete or not current. "
                "No later admission mutation is enabled by this E4 slice."
            ),
        }

    if str(refreshed.get("reasonCode") or "") == "REGISTRATION_SCAN_PENDING":
        try:
            boundary = executor.revalidate(refreshed, steps[index])
            _require_fresh_boundary(refreshed, boundary)
        except AutomaticExecutionBlocked as exc:
            blocked = block_recovery_plan(plan_id, str(exc))
            return {
                "ok": False, "state": "blocked", "plan": blocked,
                "reasonCode": exc.reason_code, "externalMutationAttempted": False,
            }
        if boundary["registrationState"] == "scan_required":
            waiting = schedule_recovery_retry(
                plan_id, "Bindery registration is still pending; no scan was repeated.",
            )
            return {
                "ok": waiting["state"] != "blocked", "state": waiting["state"],
                "plan": waiting, "externalMutationAttempted": False,
            }

    try:
        result = attempt_automatic_step(plan_id)
    except AutomaticExecutionBlocked as exc:
        if exc.reason_code in {
            "ACTION_NOT_ALLOWLISTED",
            "EXECUTOR_NOT_REGISTERED",
        }:
            raise
        blocked = block_recovery_plan(
            plan_id,
            "Known admission reconciliation stopped safely: " + str(exc),
        )
        return {
            "ok": False,
            "state": "blocked",
            "plan": blocked,
            "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED",
            "reasonCode": exc.reason_code,
            "message": str(exc),
        }

    replayed = bool(result.get("replayed"))
    reconciled = bool(result.get("reconciled"))
    execution_result = dict((result.get("execution") or {}).get("externalResult") or {})
    attempted = (
        False
        if replayed
        else bool(execution_result.get("externalMutationPerformed", True))
    )
    return {
        **result,
        "state": (
            "reconciled"
            if reconciled
            else "replayed"
            if replayed
            else "executed"
        ),
        "externalMutationAttempted": attempted,
    }


def run_automatic_cycle(limit: int = 100) -> dict[str, Any]:
    """Advance at most one E4 work item and perform at most one external mutation.

    Live E4 executors are added one mutation class at a time. This coordinator
    currently supports bounded transient acquisition retry, exact deterministic
    unsafe-media quarantine, and admission-subject registration reconciliation.
    Later recovery steps remain inert.
    """
    configured = load_automation_settings()
    if configured.automation_mode != "automatic":
        raise AutomaticExecutionBlocked(
            "AUTOMATIC_MODE_INACTIVE",
            "BOOKGUARD_AUTOMATION_MODE must be automatic.",
        )

    promoted = promote_due_recovery_retries()
    snapshot = recovery_plan_snapshot(limit)
    supported = [
        item
        for item in snapshot["items"]
        if (
            item.get("planKind") in {
                "RETRY_ACQUISITION_TRANSIENT",
                "QUARANTINE_UNSAFE_MEDIA",
            }
            or (
                item.get("planKind") == "RECONCILE_ADMISSION"
                and item.get("subjectKind") == "admission"
            )
        )
        and item.get("state") in {"planned", "ready", "retry_wait"}
    ]
    supported.sort(key=lambda item: int(item["id"]))
    candidates = [item for item in supported if item["state"] != "retry_wait"]

    if not candidates:
        if supported:
            return {
                "ok": True,
                "state": "retry_wait",
                "plan": supported[0],
                "externalMutationAttempted": False,
                "message": "The bounded retry interval has not elapsed.",
            }
        return {
            "ok": True,
            "state": "idle",
            "promotedPlanIds": [int(item["id"]) for item in promoted],
            "externalMutationAttempted": False,
            "message": "No supported E4 recovery plan is ready for automatic work.",
        }

    plan = candidates[0]
    plan_id = int(plan["id"])

    if str(plan.get("planKind") or "") == "QUARANTINE_UNSAFE_MEDIA":
        return _run_unsafe_quarantine_cycle(plan)

    if (
        str(plan.get("planKind") or "") == "RECONCILE_ADMISSION"
        and str(plan.get("subjectKind") or "") == "admission"
    ):
        return _run_admission_reconcile_cycle(plan)

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
        handoff = _handoff_completed_retry(refreshed)
        if handoff is not None:
            return handoff
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

    replayed = bool(result.get("replayed"))
    reconciled = bool(result.get("reconciled"))
    return {
        **result,
        "state": (
            "reconciled"
            if reconciled
            else "replayed"
            if replayed
            else "executed"
        ),
        "externalMutationAttempted": not replayed,
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
