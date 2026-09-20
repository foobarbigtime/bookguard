from __future__ import annotations

import json
from typing import Any

from .db import local_conn, result_by_id, utc_now


_TABLES = {
    "ebook_acquisitions",
    "ebook_admissions",
    "cleanup_actions",
    "metadata_repairs",
    "content_verifications",
    "hardlink_corrections",
    "hardlink_alias_cleanups",
    "triage_decisions",
}


def _table_exists(conn, table: str) -> bool:
    if table not in _TABLES:
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _book_context(result_id: int | None) -> dict[str, str]:
    if not result_id:
        return {"title": "", "author": ""}
    result = result_by_id(int(result_id))
    if not result:
        return {"title": "", "author": ""}
    return {
        "title": str(result.get("title") or ""),
        "author": str(result.get("author") or ""),
    }


def _event(
    *,
    kind: str,
    label: str,
    record_id: int | str,
    status: str,
    timestamp: str | None,
    title: str = "",
    author: str = "",
    path: str = "",
    message: str = "",
    detail_href: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "kindLabel": label,
        "id": record_id,
        "status": str(status or "").casefold(),
        "timestamp": timestamp or "",
        "title": title,
        "author": author,
        "path": path,
        "message": message,
        "detailHref": detail_href,
    }


def _acquisition_events(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "ebook_acquisitions"):
        return []
    rows = conn.execute(
        """
        SELECT id, result_id, status, candidate_title, observed_relative_path,
               staged_relative_path, created_at, updated_at, error
        FROM ebook_acquisitions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        book = _book_context(row["result_id"])
        path = str(row["staged_relative_path"] or row["observed_relative_path"] or "")
        items.append(
            _event(
                kind="acquisition",
                label="Acquisition",
                record_id=row["id"],
                status=row["status"],
                timestamp=row["updated_at"] or row["created_at"],
                title=book["title"],
                author=book["author"],
                path=path,
                message=str(row["error"] or row["candidate_title"] or ""),
                detail_href=f"/history/acquisition/{row['id']}",
            )
        )
    return items


def _admission_events(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "ebook_admissions"):
        return []
    rows = conn.execute(
        """
        SELECT id, result_id, status, stored_path, local_path,
               publication_method, created_at, updated_at, error
        FROM ebook_admissions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        book = _book_context(row["result_id"])
        items.append(
            _event(
                kind="admission",
                label="Admission",
                record_id=row["id"],
                status=row["status"],
                timestamp=row["updated_at"] or row["created_at"],
                title=book["title"],
                author=book["author"],
                path=str(row["stored_path"] or row["local_path"] or ""),
                message=str(row["error"] or row["publication_method"] or ""),
                detail_href=f"/history/admission/{row['id']}",
            )
        )
    return items


def _cleanup_events(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "cleanup_actions"):
        return []
    rows = conn.execute(
        """
        SELECT id, action_kind, status, author, title, stored_path, local_path,
               created_at, completed_at, error
        FROM cleanup_actions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        action = str(row["action_kind"] or "").upper()
        label = (
            "Quarantine"
            if "QUARANTINE" in action
            else "Detach"
            if "DETACH" in action
            else "Cleanup"
        )
        items.append(
            _event(
                kind="cleanup",
                label=label,
                record_id=row["id"],
                status=row["status"],
                timestamp=row["completed_at"] or row["created_at"],
                title=str(row["title"] or ""),
                author=str(row["author"] or ""),
                path=str(row["stored_path"] or row["local_path"] or ""),
                message=str(row["error"] or action.replace("_", " ").title()),
                detail_href=f"/history/cleanup/{row['id']}",
            )
        )
    return items


def _repair_events(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "metadata_repairs"):
        return []
    rows = conn.execute(
        """
        SELECT id, result_id, repair_kind, status, stored_path, local_path,
               created_at, completed_at, undone_at, error
        FROM metadata_repairs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        book = _book_context(row["result_id"])
        timestamp = row["undone_at"] or row["completed_at"] or row["created_at"]
        items.append(
            _event(
                kind="repair",
                label="Metadata repair",
                record_id=row["id"],
                status=row["status"],
                timestamp=timestamp,
                title=book["title"],
                author=book["author"],
                path=str(row["stored_path"] or row["local_path"] or ""),
                message=str(row["error"] or str(row["repair_kind"] or "").replace("_", " ").title()),
                detail_href=f"/history/repair/{row['id']}",
            )
        )
    return items


def _verification_events(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "content_verifications"):
        return []
    rows = conn.execute(
        """
        SELECT id, verdict, confidence, source, author, title, target_path,
               created_at, updated_at
        FROM content_verifications
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        _event(
            kind="verification",
            label="Verification",
            record_id=row["id"],
            status=row["verdict"],
            timestamp=row["updated_at"] or row["created_at"],
            title=str(row["title"] or ""),
            author=str(row["author"] or ""),
            path=str(row["target_path"] or ""),
            message=f"{int(row['confidence'])}% confidence via {row['source']}",
            detail_href=f"/history/verification/{row['id']}",
        )
        for row in rows
    ]


def _hardlink_events(conn, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if _table_exists(conn, "hardlink_corrections"):
        rows = conn.execute(
            """
            SELECT id, file_id, snapshot_json, status, created_at, completed_at, error
            FROM hardlink_corrections
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            path = ""
            try:
                snapshot = json.loads(row["snapshot_json"] or "{}")
                path = str(snapshot.get("source") or snapshot.get("destination") or "")
            except json.JSONDecodeError:
                pass
            items.append(
                _event(
                    kind="hardlink_correction",
                    label="Hard-link correction",
                    record_id=row["id"],
                    status=row["status"],
                    timestamp=row["completed_at"] or row["created_at"],
                    path=path,
                    message=str(row["error"] or f"Bindery file #{row['file_id']}"),
                    detail_href=f"/history/hardlink-correction/{row['id']}",
                )
            )
    if _table_exists(conn, "hardlink_alias_cleanups"):
        rows = conn.execute(
            """
            SELECT correction_id, snapshot_json, status, created_at, completed_at, error
            FROM hardlink_alias_cleanups
            ORDER BY correction_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            path = ""
            try:
                snapshot = json.loads(row["snapshot_json"] or "{}")
                path = str(snapshot.get("source") or snapshot.get("destination") or "")
            except json.JSONDecodeError:
                pass
            items.append(
                _event(
                    kind="hardlink_cleanup",
                    label="Staging-link cleanup",
                    record_id=row["correction_id"],
                    status=row["status"],
                    timestamp=row["completed_at"] or row["created_at"],
                    path=path,
                    message=str(row["error"] or "Hard-link alias cleanup"),
                    detail_href=f"/history/hardlink-cleanup/{row['correction_id']}",
                )
            )
    return items


def _triage_events(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "triage_decisions"):
        return []
    rows = conn.execute(
        """
        SELECT id, decision, author, title, stored_path, created_at, updated_at
        FROM triage_decisions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        _event(
            kind="triage",
            label="Triage decision",
            record_id=row["id"],
            status=row["decision"],
            timestamp=row["updated_at"] or row["created_at"],
            title=str(row["title"] or ""),
            author=str(row["author"] or ""),
            path=str(row["stored_path"] or ""),
            message="Durable operator triage decision.",
            detail_href=f"/history/triage/{row['id']}",
        )
        for row in rows
    ]


def operation_history(limit: int = 250) -> dict[str, Any]:
    """Read durable operation records without creating tables or mutating state."""
    limit = max(1, min(int(limit), 1000))
    per_source = min(limit, 500)

    with local_conn() as conn:
        items = (
            _acquisition_events(conn, per_source)
            + _admission_events(conn, per_source)
            + _cleanup_events(conn, per_source)
            + _repair_events(conn, per_source)
            + _verification_events(conn, per_source)
            + _hardlink_events(conn, per_source)
            + _triage_events(conn, per_source)
        )

    items.sort(
        key=lambda item: (str(item.get("timestamp") or ""), str(item.get("kind") or ""), str(item.get("id") or "")),
        reverse=True,
    )
    items = items[:limit]
    return {
        "generatedAt": utc_now(),
        "count": len(items),
        "items": items,
    }
