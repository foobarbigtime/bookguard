"""Read-only Attention entries for E4 receipts left running after interruption."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .db import local_conn


_STALE_AFTER = timedelta(minutes=15)


def _stale(updated_at: str, now: datetime) -> bool:
    try:
        recorded = datetime.fromisoformat(str(updated_at or "").replace("Z", "+00:00"))
        return recorded.tzinfo is None or now - recorded.astimezone(timezone.utc) >= _STALE_AFTER
    except (TypeError, ValueError, OverflowError):
        return True


def stale_execution_items(limit: int = 200, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Report old running receipts; never infer completion or retry an action."""
    current = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), 500))
    with local_conn() as conn:
        tables = conn.execute(
            """SELECT name FROM sqlite_master WHERE type='table'
               AND name IN ('automatic_executions', 'recovery_plans')"""
        ).fetchall()
        if {row["name"] for row in tables} != {"automatic_executions", "recovery_plans"}:
            return []
        rows = conn.execute(
            """SELECT e.id, e.plan_id, e.action_code,
                      e.updated_at, p.subject_kind,
                      p.title, p.author
               FROM automatic_executions AS e
               LEFT JOIN recovery_plans AS p ON p.id=e.plan_id
               WHERE e.state='running'
               ORDER BY e.updated_at ASC, e.id ASC LIMIT ?""",
            (limit,),
        ).fetchall()

    items = []
    for row in rows:
        if not _stale(str(row["updated_at"] or ""), current):
            continue
        action = str(row["action_code"] or "").replace("_", " ")
        items.append({
            "kind": "automatic_execution",
            "kindLabel": "Automatic execution",
            "id": row["id"],
            "status": "running",
            "title": str(row["title"] or ""),
            "author": str(row["author"] or ""),
            "message": f"The receipt for {action} remains running; its outcome is unproven.",
            "guidance": {
                "label": "Execution outcome needs review",
                "why": "The durable running receipt has not been reconciled for at least 15 minutes.",
                "nextStep": "Inspect the plan and execution journal before resuming automatic work. Do not manually repeat the action.",
                "recordedError": "",
            },
            "updatedAt": row["updated_at"],
            "detailHref": f"/history/recovery-plan/{row['plan_id']}" if row["subject_kind"] else "",
            "href": (
                "/triage#acquisitionPanel"
                if row["subject_kind"] in {"acquisition", "admission"}
                else "/triage"
            ),
        })
    return items
