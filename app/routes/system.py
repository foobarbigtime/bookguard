from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import __version__
from ..config import Settings, settings
from ..db import (
    clear_persisted_settings,
    latest_counts,
    latest_reason_counts,
    latest_recent_results,
    latest_scan,
    save_persisted_settings,
)
from ..scanner import (
    current_scan_detail,
    request_immediate_stop,
    request_safe_cancel,
    start_scan,
)
from ..services.dashboard import compact_result, scan_timing
from .models import ConfirmationRequest, require_confirmation


router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    return {"status": "ok", "version": __version__}


@router.post("/api/scan")
def api_scan(payload: ConfirmationRequest):
    require_confirmation(payload, "SCAN")
    try:
        scan_id = start_scan()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not scan_id:
        raise HTTPException(status_code=409, detail="A scan is already running.")
    return {
        "scan_id": scan_id,
        "message": (
            "Library scanning can take a while, especially for multi-file audiobooks. "
            "BookGuard reports live book and audio-file progress while it works."
        ),
    }


@router.post("/api/scan/cancel-safe")
def api_scan_cancel_safe(payload: ConfirmationRequest):
    require_confirmation(payload, "CANCEL")
    if not request_safe_cancel():
        raise HTTPException(status_code=409, detail="There is no running scan to cancel.")
    return {
        "ok": True,
        "message": "Safe cancel requested. BookGuard will finish the current book and stop before the next one.",
    }


@router.post("/api/scan/stop-immediately")
def api_scan_stop_immediately(payload: ConfirmationRequest):
    require_confirmation(payload, "STOP")
    if not request_immediate_stop():
        raise HTTPException(status_code=409, detail="There is no running scan to stop.")
    return {
        "ok": True,
        "message": (
            "Immediate stop requested. Active audiobook ffprobe work will be terminated and the "
            "interrupted book will not be saved as a completed result."
        ),
    }


@router.get("/api/status")
def api_status():
    scan = latest_scan()
    counts = latest_counts()
    recent = [compact_result(row) for row in latest_recent_results(settings.live_results_limit)]
    scan_id = str(scan.get("id")) if scan and scan.get("id") else None
    return {
        "scan": scan,
        "scan_detail": current_scan_detail(scan_id),
        "counts": counts,
        "review_reasons": latest_reason_counts("REVIEW"),
        "timing": scan_timing(scan),
        "recent_results": recent,
        "poll_ms": settings.dashboard_poll_ms,
        "repair_mode": settings.metadata_repair_mode,
    }


@router.get("/api/settings")
def api_get_settings():
    return settings.public_dict()


@router.post("/api/settings")
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

    if payload.get("clear_api_key"):
        payload["bindery_api_key"] = ""
    elif not str(payload.get("bindery_api_key", "")).strip():
        payload.pop("bindery_api_key", None)
    payload.pop("clear_api_key", None)

    settings.apply(payload)
    save_persisted_settings(settings.persistable_dict())
    return {"ok": True, "settings": settings.public_dict()}


@router.post("/api/settings/reset")
def api_reset_settings(payload: ConfirmationRequest):
    require_confirmation(payload, "RESET")
    scan = latest_scan()
    if scan and scan.get("status") == "running":
        raise HTTPException(status_code=409, detail="Wait for the current scan to finish before resetting settings.")
    defaults = Settings()
    settings.__dict__.update(defaults.__dict__)
    clear_persisted_settings()
    return {"ok": True, "settings": settings.public_dict()}
