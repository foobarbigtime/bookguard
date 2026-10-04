from __future__ import annotations

from collections import Counter
from typing import Any

from .attention_execution import stale_execution_items
from .operator_guidance import operation_guidance
from .observe import observe_attention_items
from .db import (
    local_conn,
    result_by_id,
    utc_now,
)


_RESOLVED_JOURNAL_STATUSES = {"applied", "cancelled"}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _journal_rows(table: str, id_column: str) -> list[dict[str, Any]]:
    """Read unresolved hard-link journal rows without creating tables or changing state."""
    allowed = {
        "hardlink_corrections": "id",
        "hardlink_alias_cleanups": "correction_id",
    }
    if allowed.get(table) != id_column:
        raise ValueError("Unsupported attention journal table.")

    with local_conn() as conn:
        if not _table_exists(conn, table):
            return []
        rows = conn.execute(
            f"""
            SELECT {id_column} AS id, status, created_at, completed_at, error
            FROM {table}
            ORDER BY {id_column} DESC
            LIMIT 500
            """
        ).fetchall()
    return [
        dict(row)
        for row in rows
        if str(row["status"] or "").casefold() not in _RESOLVED_JOURNAL_STATUSES
    ]


# Records left by the removed self-download workflows (acquisition, admission).
# BookGuard no longer advances them, so an unfinished one stays visible with a
# link to its Activity record until someone checks it in Bindery.
_LEGACY_ATTENTION = {
    "ebook_acquisitions": ("acquisition", "Acquisition", {"review_required", "finalizing", "cleanup_required", "failed"}),
    "ebook_admissions": ("admission", "Admission", {"registration_conflict", "registration_correcting", "failed"}),
}


def _legacy_items(limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with local_conn() as conn:
        for table, (kind, label, statuses) in _LEGACY_ATTENTION.items():
            if not _table_exists(conn, table):
                continue
            rows = conn.execute(
                f"SELECT id, result_id, status, error, created_at, updated_at FROM {table} "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            for row in rows:
                status = str(row["status"] or "").casefold()
                if status not in statuses:
                    continue
                result = result_by_id(int(row["result_id"])) if row["result_id"] else None
                error = str(row["error"] or "").strip()
                detail = f"/activity/{kind}/{row['id']}"
                items.append({
                    "kind": kind,
                    "kindLabel": label,
                    "id": row["id"],
                    "status": status,
                    "title": str((result or {}).get("title") or ""),
                    "author": str((result or {}).get("author") or ""),
                    "message": error or f"An older BookGuard left this {kind} unfinished ({status.replace('_', ' ')}).",
                    "guidance": {
                        "label": "Left over from a removed feature",
                        "why": "BookGuard no longer downloads or places files itself, so it will not finish this.",
                        "nextStep": "Open the Activity record, then check the book, its files and any download in Bindery.",
                        "recordedError": error,
                    },
                    "updatedAt": row["updated_at"] or row["created_at"],
                    "detailHref": detail,
                    "href": detail,
                })
    return items


def _hardlink_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in _journal_rows("hardlink_corrections", "id"):
        items.append(
            {
                "kind": "hardlink_correction",
                "kindLabel": "Hard-link correction",
                "id": row["id"],
                "status": str(row.get("status") or "").casefold(),
                "title": "",
                "author": "",
                "message": str(row.get("error") or "").strip()
                or "An interrupted or uncertain shared-file correction needs review.",
                "guidance": operation_guidance(
                    "hardlink_correction",
                    str(row.get("status") or ""),
                    str(row.get("error") or ""),
                ),
                "updatedAt": row.get("completed_at") or row.get("created_at"),
                "detailHref": f"/activity/hardlink-correction/{row['id']}",
                "href": "/review/triage#hardlinkPanel",
            }
        )
    for row in _journal_rows("hardlink_alias_cleanups", "correction_id"):
        items.append(
            {
                "kind": "hardlink_cleanup",
                "kindLabel": "Staging-link cleanup",
                "id": row["id"],
                "status": str(row.get("status") or "").casefold(),
                "title": "",
                "author": "",
                "message": str(row.get("error") or "").strip()
                or "An interrupted staging-link cleanup needs review.",
                "guidance": operation_guidance(
                    "hardlink_cleanup",
                    str(row.get("status") or ""),
                    str(row.get("error") or ""),
                ),
                "updatedAt": row.get("completed_at") or row.get("created_at"),
                "detailHref": f"/activity/hardlink-cleanup/{row['id']}",
                "href": "/review/triage#hardlinkPanel",
            }
        )
    return items


def _blocked_plan_items(limit: int) -> list[dict[str, Any]]:
    """Show current E4 refusals without changing or retrying their plans."""
    with local_conn() as conn:
        if not _table_exists(conn, "recovery_plans"):
            return []
        rows = conn.execute(
            """SELECT id, subject_kind, title, author, plan_kind,
                      reason_code, last_error, updated_at
               FROM recovery_plans WHERE state='blocked'
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    items = []
    for row in rows:
        reason = str(row["last_error"] or "").strip()
        plan_kind = str(row["plan_kind"] or "").replace("_", " ").title()
        items.append({
            "kind": "recovery_plan",
            "kindLabel": "Automatic recovery plan",
            "id": row["id"],
            "status": "blocked",
            "title": str(row["title"] or ""),
            "author": str(row["author"] or ""),
            "message": reason or f"{plan_kind} was blocked by a safety check.",
            "guidance": {
                "label": "Automatic step refused",
                "why": reason or str(row["reason_code"] or "Safety proof was not recorded."),
                "nextStep": "Review the plan audit and current evidence before any new Observe cycle.",
                "recordedError": reason,
            },
            "updatedAt": row["updated_at"],
            "detailHref": f"/activity/recovery-plan/{row['id']}",
            "href": "/review",
        })
    return items


def attention_snapshot(limit: int = 200) -> dict[str, Any]:
    """Return durable, read-only operator-attention state across guarded workflows."""
    limit = max(1, min(int(limit), 500))
    items = (
        _legacy_items(limit)
        + _hardlink_items()
        + _blocked_plan_items(limit)
        + stale_execution_items(limit)
        + observe_attention_items(limit)
    )
    items.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
    counts = Counter(item["kind"] for item in items)
    return {
        "generatedAt": utc_now(),
        "total": len(items),
        "items": items,
        "summary": {
            "legacy": counts["acquisition"] + counts["admission"],
            "hardlinkCorrections": counts["hardlink_correction"],
            "hardlinkCleanups": counts["hardlink_cleanup"],
            "recoveryPlans": counts["recovery_plan"],
            "runningExecutions": counts["automatic_execution"],
            "observe": counts["observe"],
        },
    }
