from __future__ import annotations

from datetime import datetime, timezone

from ..actions import missing_detach_preview
from ..config import settings
from ..db import latest_results, latest_scan
from ..repair import RepairError, build_repair_preview, repair_candidate_summary


def scan_timing(scan: dict | None) -> dict:
    if not scan:
        return {"percent": 0.0, "elapsed_seconds": 0, "eta_seconds": None}
    try:
        started = datetime.fromisoformat(scan["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        if scan.get("finished_at"):
            end = datetime.fromisoformat(scan["finished_at"])
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (end - started).total_seconds())
    except Exception:
        elapsed = 0.0

    total = int(scan.get("total") or 0)
    processed = int(scan.get("processed") or 0)
    percent = (processed / total * 100.0) if total else 100.0
    eta = None
    if scan.get("status") == "running" and processed > 0 and elapsed > 0 and total > processed:
        rate = processed / elapsed
        if rate > 0:
            eta = int((total - processed) / rate)
    return {
        "percent": round(percent, 1),
        "elapsed_seconds": int(elapsed),
        "eta_seconds": eta,
    }


def _detected_fields(row: dict) -> tuple[str, str, str]:
    metadata = row.get("metadata") or {}
    if row.get("format") == "audiobook":
        return (
            str(metadata.get("detected_title") or ""),
            str(metadata.get("detected_author") or ""),
            str(metadata.get("detected_genre") or ""),
        )
    return (
        str(metadata.get("title") or ""),
        str(metadata.get("author") or ""),
        "",
    )


def missing_cleanup_state(row: dict) -> dict:
    if row.get("classification") != "MISSING":
        return {
            "eligible": False,
            "safe": False,
            "state": "not_missing",
            "reason": "",
        }
    try:
        return missing_detach_preview(row)
    except Exception as exc:
        return {
            "eligible": False,
            "safe": False,
            "state": "preview_error",
            "reason": str(exc)[:500],
        }


def compact_result(row: dict) -> dict:
    detected_title, detected_author, detected_genre = _detected_fields(row)
    return {
        "id": row["id"],
        "risk_score": row["risk_score"],
        "classification": row["classification"],
        "reason_code": row.get("reason_code") or "UNKNOWN",
        "author": row["author"],
        "title": row["title"],
        "format": row["format"],
        "reasons": row["reasons"],
        "stored_path": row["stored_path"],
        "detected_title": detected_title,
        "detected_author": detected_author,
        "detected_genre": detected_genre,
        "repair": repair_candidate_summary(row),
        "missing_cleanup": missing_cleanup_state(row),
    }


def enrich_results(rows: list[dict]) -> list[dict]:
    return [{**row, **compact_result(row)} for row in rows]


def latest_repair_candidates(limit: int = 500) -> list[dict]:
    """Return only actually actionable safe repairs from the latest scan."""
    if settings.metadata_repair_mode == "off":
        return []
    rows = latest_results(limit=10000)
    candidates: list[dict] = []
    for row in rows:
        enriched = {**row, **compact_result(row)}
        summary = enriched["repair"]
        if not summary.get("eligible") or not summary.get("safe"):
            continue

        try:
            preview = build_repair_preview(row)
        except RepairError:
            continue
        if not preview.get("eligible") or not preview.get("safe"):
            continue

        enriched["repair"] = {
            **summary,
            "kind": preview.get("kind") or summary.get("kind", ""),
            "reason": preview.get("reason") or summary.get("reason", ""),
        }
        candidates.append(enriched)
    candidates.sort(
        key=lambda row: (
            0 if row.get("reason_code") == "SWAPPED_METADATA" else 1,
            str(row.get("author") or "").casefold(),
            str(row.get("title") or "").casefold(),
        )
    )
    return candidates[:limit]


def latest_missing_cleanup() -> dict:
    scan = latest_scan()
    if not scan:
        return {
            "scan_id": "",
            "scan_status": "none",
            "total": 0,
            "safe_count": 0,
            "attention_count": 0,
            "already_detached_count": 0,
            "items": [],
        }

    rows = latest_results(classification="MISSING", limit=10000)
    items = []
    safe_count = 0
    attention_count = 0
    already_detached_count = 0

    for row in rows:
        state = missing_cleanup_state(row)
        if state.get("safe"):
            safe_count += 1
        elif state.get("state") == "already_detached":
            already_detached_count += 1
        else:
            attention_count += 1
        items.append({
            "id": row["id"],
            "file_id": row["file_id"],
            "book_id": row["book_id"],
            "author": row["author"],
            "title": row["title"],
            "format": row["format"],
            "stored_path": row["stored_path"],
            "local_path": row["local_path"],
            **state,
        })

    return {
        "scan_id": scan["id"],
        "scan_status": scan["status"],
        "total": len(rows),
        "safe_count": safe_count,
        "attention_count": attention_count,
        "already_detached_count": already_detached_count,
        "items": items,
    }
