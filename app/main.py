from __future__ import annotations

from datetime import datetime, timezone
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .actions import (
    ActionError,
    detach,
    detach_missing,
    missing_detach_preview,
    quarantine,
)
from .config import Settings, settings
from .db import (
    clear_persisted_settings,
    init_local_db,
    latest_counts,
    latest_reason_counts,
    latest_recent_results,
    latest_results,
    latest_scan,
    load_persisted_settings,
    recent_cleanup_actions,
    recent_metadata_repairs,
    result_by_id,
    save_persisted_settings,
)
from .repair import (
    RepairError,
    apply_metadata_repair,
    build_repair_preview,
    repair_candidate_summary,
    undo_metadata_repair,
)
from .scanner import start_scan


app = FastAPI(title="BookGuard", version="0.4.5")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup() -> None:
    init_local_db()
    settings.apply(load_persisted_settings())
    if settings.scan_on_start:
        start_scan()


@app.get("/health")
def health():
    return {"status": "ok", "version": app.version}


def _scan_timing(scan: dict | None) -> dict:
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


def _missing_cleanup_state(row: dict) -> dict:
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


def _compact_result(row: dict) -> dict:
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
        "missing_cleanup": _missing_cleanup_state(row),
    }


def _enrich_results(rows: list[dict]) -> list[dict]:
    return [{**row, **_compact_result(row)} for row in rows]


def _latest_repair_candidates(limit: int = 500) -> list[dict]:
    """Return only actually actionable safe repairs from the latest scan."""
    if settings.metadata_repair_mode == "off":
        return []
    rows = latest_results(limit=10000)
    candidates: list[dict] = []
    for row in rows:
        enriched = {**row, **_compact_result(row)}
        summary = enriched["repair"]
        if not summary.get("eligible") or not summary.get("safe"):
            continue

        # The cheap scan-level summary is only a gate. A detailed preview reads
        # the real file metadata and can discover that a proposed repair is a
        # no-op. Never show those as repair candidates.
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


def _latest_missing_cleanup() -> dict:
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
        state = _missing_cleanup_state(row)
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


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    classification: str | None = None,
    reason_code: str | None = None,
):
    scan = latest_scan()
    counts = latest_counts()
    results = latest_results(
        classification=classification,
        reason_code=reason_code,
        limit=settings.dashboard_result_limit,
    )
    missing_cleanup = _latest_missing_cleanup() if classification == "MISSING" else None
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "scan": scan,
            "timing": _scan_timing(scan),
            "counts": counts,
            "review_reasons": latest_reason_counts("REVIEW"),
            "results": _enrich_results(results),
            "classification": classification or "",
            "reason_code": reason_code or "",
            "allow_actions": settings.allow_actions,
            "repair_mode": settings.metadata_repair_mode,
            "bindery_db_exists": os.path.exists(settings.bindery_db),
            "sample_files": settings.sample_files,
            "poll_ms": settings.dashboard_poll_ms,
            "missing_cleanup": missing_cleanup,
            "cleanup_history": recent_cleanup_actions(50) if classification == "MISSING" else [],
            "version": app.version,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    values = settings.public_dict()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": values,
            "config_dir": settings.config_dir,
            "bindery_db_exists": os.path.exists(settings.bindery_db),
            "audiobook_root_exists": os.path.exists(settings.audiobook_root),
            "ebook_root_exists": os.path.exists(settings.ebook_root),
            "quarantine_root_exists": os.path.exists(settings.quarantine_root),
            "scan": latest_scan(),
            "version": app.version,
        },
    )


@app.get("/repairs", response_class=HTMLResponse)
def repairs_page(request: Request):
    candidates = _latest_repair_candidates()
    return templates.TemplateResponse(
        request=request,
        name="repairs.html",
        context={
            "candidates": candidates,
            "candidate_count": len(candidates),
            "repairs": recent_metadata_repairs(200),
            "repair_mode": settings.metadata_repair_mode,
            "version": app.version,
        },
    )


@app.post("/api/scan")
def api_scan():
    try:
        scan_id = start_scan()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not scan_id:
        raise HTTPException(status_code=409, detail="A scan is already running.")
    return {"scan_id": scan_id}


@app.get("/api/status")
def api_status():
    scan = latest_scan()
    counts = latest_counts()
    recent = [_compact_result(row) for row in latest_recent_results(settings.live_results_limit)]
    return {
        "scan": scan,
        "counts": counts,
        "review_reasons": latest_reason_counts("REVIEW"),
        "timing": _scan_timing(scan),
        "recent_results": recent,
        "poll_ms": settings.dashboard_poll_ms,
        "repair_mode": settings.metadata_repair_mode,
    }


@app.get("/api/settings")
def api_get_settings():
    return settings.public_dict()


@app.post("/api/settings")
async def api_save_settings(request: Request):
    scan = latest_scan()
    if scan and scan.get("status") == "running":
        raise HTTPException(status_code=409, detail="Wait for the current scan to finish before changing settings.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid settings payload.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid settings payload.")

    # Blank API key means "keep the existing key" unless explicitly cleared.
    if payload.get("clear_api_key"):
        payload["bindery_api_key"] = ""
    elif not str(payload.get("bindery_api_key", "")).strip():
        payload.pop("bindery_api_key", None)
    payload.pop("clear_api_key", None)

    settings.apply(payload)
    save_persisted_settings(settings.persistable_dict())
    return {"ok": True, "settings": settings.public_dict()}


@app.post("/api/settings/reset")
def api_reset_settings():
    scan = latest_scan()
    if scan and scan.get("status") == "running":
        raise HTTPException(status_code=409, detail="Wait for the current scan to finish before resetting settings.")
    defaults = Settings()
    settings.__dict__.update(defaults.__dict__)
    clear_persisted_settings()
    return {"ok": True, "settings": settings.public_dict()}


@app.get("/api/results/{result_id}/repair-preview")
def api_repair_preview(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    try:
        preview = build_repair_preview(item)
    except RepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"result_id": result_id, "mode": settings.metadata_repair_mode, **preview}


@app.post("/api/results/{result_id}/repair")
def api_repair(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    try:
        return {"ok": True, **apply_metadata_repair(item)}
    except RepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/repairs/{repair_id}/undo")
def api_undo_repair(repair_id: int):
    try:
        return undo_metadata_repair(repair_id)
    except RepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/results/{result_id}/missing-detach-preview")
def api_missing_detach_preview(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    try:
        return {"result_id": result_id, **missing_detach_preview(item)}
    except ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/missing/preview")
def api_missing_preview():
    return _latest_missing_cleanup()


@app.post("/api/results/{result_id}/detach-missing")
async def api_detach_missing(result_id: int, request: Request):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("confirm") != "DETACH":
        raise HTTPException(status_code=400, detail="Explicit DETACH confirmation is required.")
    try:
        cleanup_id = detach_missing(item)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "ok": True,
        "cleanup_id": cleanup_id,
        "message": "Stale Bindery association detached. No physical file was deleted or moved.",
    }


@app.post("/api/missing/detach-all")
async def api_detach_all_missing(request: Request):
    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise HTTPException(status_code=409, detail="A completed scan is required before MISSING cleanup.")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("confirm") != "DETACH":
        raise HTTPException(status_code=400, detail="Explicit DETACH confirmation is required.")
    if str(payload.get("scan_id") or "") != scan["id"]:
        raise HTTPException(status_code=409, detail="The scan changed. Refresh the MISSING preview before cleanup.")

    rows = latest_results(classification="MISSING", limit=10000)
    if not rows:
        return {"ok": True, "detached": 0, "cleanup_ids": [], "scan_id": None}

    # Full preflight before the first mutation.
    previews = [(row, missing_detach_preview(row)) for row in rows]
    unsafe = [(row, preview) for row, preview in previews if not preview.get("safe")]
    if unsafe:
        first_row, first_preview = unsafe[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bulk cleanup refused: {len(unsafe)} of {len(rows)} MISSING results are not safe. "
                f"First issue: {first_row['author']} — {first_row['title']}: "
                f"{first_preview.get('reason', 'Safety check failed.')}"
            ),
        )

    cleanup_ids: list[int] = []
    for index, (row, _) in enumerate(previews, start=1):
        try:
            cleanup_ids.append(detach_missing(row))
        except ActionError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Bulk cleanup stopped after {len(cleanup_ids)} of {len(rows)} successful detaches. "
                    f"Item {index} failed: {row['author']} — {row['title']}: {exc}"
                ),
            )

    validation_scan_id = start_scan()
    return {
        "ok": True,
        "detached": len(cleanup_ids),
        "cleanup_ids": cleanup_ids,
        "scan_id": validation_scan_id,
        "message": (
            f"Detached {len(cleanup_ids)} stale Bindery association(s). "
            "No physical files were deleted or moved. A validation scan was started."
        ),
    }


@app.post("/api/results/{result_id}/detach")
def api_detach(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    if item["classification"] == "PASS":
        raise HTTPException(status_code=400, detail="Refusing to detach a PASS result.")
    try:
        detach(item["book_id"], item["stored_path"])
    except ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/api/results/{result_id}/quarantine")
def api_quarantine(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    if item["classification"] == "PASS":
        raise HTTPException(status_code=400, detail="Refusing to quarantine a PASS result.")
    try:
        destination = quarantine(item["book_id"], item["stored_path"], item["local_path"])
    except ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "destination": destination}
