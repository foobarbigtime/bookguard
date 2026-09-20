from __future__ import annotations

from collections import Counter
from typing import Any

from .acquisition_coordinator import acquisition_coordinator_status
from .operator_guidance import explain_blockers, operation_guidance
from .db import (
    local_conn,
    recent_ebook_acquisitions,
    recent_ebook_admissions,
    result_by_id,
    utc_now,
)


_ACQUISITION_ATTENTION = {
    "review_required",
    "finalizing",
    "cleanup_required",
    "failed",
}
_ADMISSION_ATTENTION = {
    "registration_conflict",
    "registration_correcting",
    "failed",
}
_RESOLVED_JOURNAL_STATUSES = {"applied", "cancelled"}


def _book_context(result_id: int | None) -> dict[str, Any]:
    if not result_id:
        return {"title": "", "author": ""}
    result = result_by_id(int(result_id))
    if not result:
        return {"title": "", "author": ""}
    return {
        "title": str(result.get("title") or ""),
        "author": str(result.get("author") or ""),
    }


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


def _acquisition_items(limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in recent_ebook_acquisitions(limit):
        status = str(row.get("status") or "").casefold()
        if status not in _ACQUISITION_ATTENTION:
            continue
        book = _book_context(row.get("result_id"))
        error = str(row.get("error") or "").strip()
        items.append(
            {
                "kind": "acquisition",
                "kindLabel": "Acquisition",
                "id": row.get("id"),
                "status": status,
                "title": book["title"],
                "author": book["author"],
                "message": error
                or "This replacement workflow requires operator review or recovery.",
                "guidance": operation_guidance("acquisition", status, error),
                "updatedAt": row.get("updated_at") or row.get("created_at"),
                "detailHref": f"/history/acquisition/{row.get('id')}",
                "href": "/triage#acquisitionPanel",
            }
        )
    return items


def _admission_items(limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in recent_ebook_admissions(limit):
        status = str(row.get("status") or "").casefold()
        if status not in _ADMISSION_ATTENTION:
            continue
        book = _book_context(row.get("result_id"))
        default = (
            "Bindery registered the admitted ebook to a different book."
            if status == "registration_conflict"
            else "A guarded registration correction was interrupted and requires explicit review."
            if status == "registration_correcting"
            else "This admission failed and requires operator review."
        )
        error = str(row.get("error") or "").strip()
        items.append(
            {
                "kind": "admission",
                "kindLabel": "Admission",
                "id": row.get("id"),
                "status": status,
                "title": book["title"],
                "author": book["author"],
                "message": error or default,
                "guidance": operation_guidance("admission", status, error),
                "updatedAt": row.get("updated_at") or row.get("created_at"),
                "detailHref": f"/history/admission/{row.get('id')}",
                "href": "/triage#acquisitionPanel",
            }
        )
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
                "detailHref": f"/history/hardlink-correction/{row['id']}",
                "href": "/triage#hardlinkPanel",
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
                "detailHref": f"/history/hardlink-cleanup/{row['id']}",
                "href": "/triage#hardlinkPanel",
            }
        )
    return items


def _coordinator_items() -> list[dict[str, Any]]:
    status = acquisition_coordinator_status()
    state = str(status.get("state") or "").casefold()
    if state not in {"attention_required", "error"}:
        return []
    blockers = [str(item) for item in status.get("blockers") or []]
    explained = explain_blockers(blockers)
    guidance = None
    if explained:
        first = explained[0]
        guidance = {
            "label": first["label"],
            "why": first["why"],
            "nextStep": first["fix"],
            "recordedError": str(status.get("lastError") or "").strip(),
        }
    return [
        {
            "kind": "coordinator",
            "kindLabel": "Coordinator",
            "id": None,
            "status": state,
            "title": "",
            "author": "",
            "message": str(status.get("lastError") or "").strip()
            or "The supervised acquisition coordinator requires operator attention.",
            "guidance": guidance,
            "updatedAt": status.get("lastRunAt"),
            "detailHref": "",
            "href": "/triage#acquisitionPanel",
        }
    ]


def attention_snapshot(limit: int = 200) -> dict[str, Any]:
    """Return durable, read-only operator-attention state across guarded workflows."""
    limit = max(1, min(int(limit), 500))
    items = (
        _acquisition_items(limit)
        + _admission_items(limit)
        + _hardlink_items()
        + _coordinator_items()
    )
    items.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
    counts = Counter(item["kind"] for item in items)
    return {
        "generatedAt": utc_now(),
        "total": len(items),
        "items": items,
        "summary": {
            "acquisitions": counts["acquisition"],
            "admissions": counts["admission"],
            "hardlinkCorrections": counts["hardlink_correction"],
            "hardlinkCleanups": counts["hardlink_cleanup"],
            "coordinator": counts["coordinator"],
        },
    }
