from __future__ import annotations

import json
from typing import Any

from .db import local_conn, result_by_id, utc_now
from .operator_guidance import operation_guidance


_TABLES = {
    "ebook_acquisitions",
    "ebook_admissions",
    "cleanup_actions",
    "metadata_repairs",
    "content_verifications",
    "hardlink_corrections",
    "hardlink_alias_cleanups",
    "triage_decisions",
    "automation_observations",
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


def _observe_events(conn, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "automation_observations"):
        return []
    rows = conn.execute(
        """
        SELECT id, decision, reason, title, author, path, last_seen_at
        FROM automation_observations
        ORDER BY last_seen_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        _event(
            kind="observe",
            label="Observe decision",
            record_id=row["id"],
            status=row["decision"],
            timestamp=row["last_seen_at"],
            title=str(row["title"] or ""),
            author=str(row["author"] or ""),
            path=str(row["path"] or ""),
            message=str(row["reason"] or ""),
            detail_href=f"/history/observe/{row['id']}",
        )
        for row in rows
    ]


def operation_history(limit: int = 250) -> dict[str, Any]:
    """Read durable operation records without creating tables or mutating state."""
    limit = max(1, min(int(limit), 1000))
    per_source = limit

    with local_conn() as conn:
        items = (
            _acquisition_events(conn, per_source)
            + _admission_events(conn, per_source)
            + _cleanup_events(conn, per_source)
            + _repair_events(conn, per_source)
            + _verification_events(conn, per_source)
            + _hardlink_events(conn, per_source)
            + _triage_events(conn, per_source)
            + _observe_events(conn, per_source)
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


_DETAIL_SPECS: dict[str, dict[str, Any]] = {
    "acquisition": {
        "table": "ebook_acquisitions",
        "pk": "id",
        "label": "Acquisition",
        "status": "status",
        "json": {"verification_json": "Verification evidence"},
        "omit": {"grab_response_json"},
    },
    "admission": {
        "table": "ebook_admissions",
        "pk": "id",
        "label": "Admission",
        "status": "status",
        "json": {"verification_json": "Verification evidence"},
        "omit": set(),
    },
    "cleanup": {
        "table": "cleanup_actions",
        "pk": "id",
        "label": "Cleanup / quarantine",
        "status": "status",
        "json": {},
        "omit": set(),
    },
    "repair": {
        "table": "metadata_repairs",
        "pk": "id",
        "label": "Metadata repair",
        "status": "status",
        "json": {
            "before_json": "Before",
            "after_json": "After",
        },
        "omit": set(),
    },
    "verification": {
        "table": "content_verifications",
        "pk": "id",
        "label": "Verification",
        "status": "verdict",
        "json": {"evidence_json": "Verification evidence"},
        "omit": set(),
    },
    "hardlink-correction": {
        "table": "hardlink_corrections",
        "pk": "id",
        "label": "Hard-link correction",
        "status": "status",
        "json": {"snapshot_json": "Correction snapshot"},
        "omit": set(),
    },
    "hardlink-cleanup": {
        "table": "hardlink_alias_cleanups",
        "pk": "correction_id",
        "label": "Staging-link cleanup",
        "status": "status",
        "json": {"snapshot_json": "Cleanup snapshot"},
        "omit": set(),
    },
    "triage": {
        "table": "triage_decisions",
        "pk": "id",
        "label": "Triage decision",
        "status": "decision",
        "json": {},
        "omit": {"signature"},
    },
    "observe": {
        "table": "automation_observations",
        "pk": "id",
        "label": "Observe decision",
        "status": "decision",
        "json": {"evidence_json": "Observed evidence"},
        "omit": {"signature"},
    },
}


def _field_label(name: str) -> str:
    special = {
        "id": "ID",
        "result_id": "Result ID",
        "scan_id": "Scan ID",
        "file_id": "File ID",
        "book_id": "Book ID",
        "queue_id": "Queue ID",
        "admission_id": "Admission ID",
        "candidate_guid": "Candidate GUID",
        "staged_sha256": "Staged SHA-256",
        "file_fingerprint": "File fingerprint",
        "observed_modified_ns": "Observed modified ns",
    }
    if name in special:
        return special[name]
    return name.replace("_", " ").strip().title()


def _decode_json_detail(raw: Any) -> tuple[Any, str | None]:
    if raw in (None, ""):
        return None, None
    try:
        return json.loads(str(raw)), None
    except json.JSONDecodeError as exc:
        return str(raw), f"Stored JSON could not be decoded: {exc}"


def _detail_book(row: dict[str, Any]) -> dict[str, str]:
    title = str(row.get("title") or "")
    author = str(row.get("author") or "")
    if title or author:
        return {"title": title, "author": author}

    result_id = row.get("result_id")
    if result_id:
        return _book_context(int(result_id))

    raw_snapshot = row.get("snapshot_json")
    if raw_snapshot:
        try:
            snapshot = json.loads(str(raw_snapshot))
        except json.JSONDecodeError:
            snapshot = {}
        if isinstance(snapshot, dict):
            retained = snapshot.get("retained")
            if isinstance(retained, dict):
                title = str(retained.get("title") or "")
                author = str(retained.get("author") or "")
                if title or author:
                    return {"title": title, "author": author}

    return {"title": "", "author": ""}


def _detail_path(row: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    for key in (
        "stored_path",
        "target_path",
        "local_path",
        "staged_relative_path",
        "observed_relative_path",
        "path",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    for block in evidence:
        value = block.get("value")
        if isinstance(value, dict):
            for key in ("source", "destination", "path"):
                candidate = str(value.get(key) or "").strip()
                if candidate:
                    return candidate
    return ""


def operation_detail(kind: str, record_id: int) -> dict[str, Any] | None:
    """Read one durable operation record without creating tables or mutating state."""
    kind = str(kind or "").casefold().strip()
    spec = _DETAIL_SPECS.get(kind)
    if not spec:
        return None

    table = str(spec["table"])
    primary_key = str(spec["pk"])
    with local_conn() as conn:
        if not _table_exists(conn, table):
            return None
        row = conn.execute(
            f"SELECT * FROM {table} WHERE {primary_key}=? LIMIT 1",
            (int(record_id),),
        ).fetchone()
    if row is None:
        return None

    raw = dict(row)
    evidence: list[dict[str, Any]] = []
    json_fields: dict[str, str] = spec["json"]
    for field, label in json_fields.items():
        value, parse_error = _decode_json_detail(raw.get(field))
        if value is None and not parse_error:
            continue
        evidence.append(
            {
                "label": label,
                "value": value,
                "parseError": parse_error,
                "pretty": (
                    json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
                    if not isinstance(value, str)
                    else value
                ),
            }
        )

    omitted = set(spec["omit"]) | set(json_fields)
    fields = []
    for name, value in raw.items():
        if name in omitted or value is None or value == "":
            continue
        fields.append(
            {
                "name": name,
                "label": _field_label(name),
                "value": value,
            }
        )

    book = _detail_book(raw)
    error = str(raw.get("error") or "").strip()
    status_field = str(spec["status"])
    status = str(raw.get(status_field) or "").casefold()
    path = _detail_path(raw, evidence)

    notes = []
    summary = ""
    if kind in {"hardlink-correction", "hardlink-cleanup"}:
        snapshot_value, _ = _decode_json_detail(raw.get("snapshot_json"))
        if isinstance(snapshot_value, dict):
            wrong = snapshot_value.get("wrong")
            retained = snapshot_value.get("retained")
            if isinstance(wrong, dict) and isinstance(retained, dict):
                wrong_title = str(wrong.get("title") or "unknown book")
                retained_title = str(retained.get("title") or "unknown book")
                summary = (
                    f"Shared EPUB association corrected away from {wrong_title} "
                    f"and retained for {retained_title}."
                )

    if kind == "acquisition" and raw.get("grab_response_json"):
        notes.append(
            "The stored grab-provider response is intentionally omitted from this "
            "read-only view because it may contain provider-specific request data."
        )

    return {
        "kind": kind,
        "kindLabel": str(spec["label"]),
        "id": raw.get(primary_key),
        "status": status,
        "title": book["title"],
        "author": book["author"],
        "path": path,
        "error": error,
        "summary": summary,
        "guidance": operation_guidance(kind, status, error),
        "fields": fields,
        "evidence": evidence,
        "notes": notes,
        "historyHref": "/history",
    }
