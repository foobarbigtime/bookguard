from __future__ import annotations

import json
from typing import Any, Protocol

from .config import AUTOMATIC_ACTION_CODES, load_automation_settings
from .db import local_conn, utc_now
from .recovery_planner import recovery_plan_by_id, record_recovery_step_success


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

    existing = _existing(str(plan["signature"]), action_code, step_index)
    if existing and existing["state"] == "succeeded":
        return {
            "ok": True,
            "replayed": True,
            "execution": existing,
            "plan": record_recovery_step_success(int(plan_id), step_index),
        }

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
