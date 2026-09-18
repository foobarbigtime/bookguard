from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..config import settings
from ..db import latest_results
from ..repair import RepairError
from ..verifier import (
    apply_verified_metadata_repair,
    start_verification_job,
    test_malware_scanner,
    test_tika,
    verification_for_result,
    verification_job_status,
    verification_summary,
    verified_repair_preview,
    verify_result,
)
from .dependencies import current_result
from .models import ConfirmationRequest, require_confirmation


router = APIRouter(prefix="/api/verification", tags=["verification"])


@router.get("/status")
def api_verification_status():
    return {"job": verification_job_status(), "summary": verification_summary()}


@router.get("/cached")
def api_verification_cached(classification: str = "REVIEW", reason_code: str | None = None):
    classification = str(classification or "REVIEW").upper()
    if classification not in {"REVIEW", "REJECT"}:
        raise HTTPException(status_code=400, detail="Use REVIEW or REJECT.")
    rows = latest_results(
        classification=classification,
        reason_code=(str(reason_code).strip() if reason_code else None),
        limit=10000,
    )
    items = {}
    for row in rows:
        verification = verification_for_result(row)
        if verification:
            items[str(row["id"])] = verification
    return {"classification": classification, "items": items}


@router.post("/start")
async def api_verification_start(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid verification request.")
    classification = str(payload.get("classification") or "REVIEW").upper()
    reason_code = str(payload.get("reason_code") or "").strip() or None
    try:
        job_id = start_verification_job(classification, reason_code)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not job_id:
        raise HTTPException(status_code=409, detail="A content verification job is already running.")
    return {"ok": True, "job_id": job_id}


@router.get("/tika-test")
def api_verification_tika_test():
    return test_tika()


@router.get("/malware-test")
def api_verification_malware_test():
    return test_malware_scanner()


@router.get("/{result_id}")
def api_verification_get(result_id: int):
    item = current_result(result_id)
    verification = verification_for_result(item)
    if not verification:
        return {"result_id": result_id, "verified": False}
    return {"result_id": result_id, "verified": True, "verification": verification}


@router.post("/{result_id}/run")
def api_verification_run(result_id: int):
    item = current_result(result_id)
    try:
        verification = verify_result(item, force=True)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "verification": verification}


@router.get("/{result_id}/repair-preview")
def api_verified_repair_preview(result_id: int):
    item = current_result(result_id)
    verification = verification_for_result(item)
    try:
        preview = verified_repair_preview(item, verification)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"result_id": result_id, "mode": settings.metadata_repair_mode, **preview}


@router.post("/{result_id}/repair")
def api_verified_repair(result_id: int, payload: ConfirmationRequest):
    require_confirmation(payload, "REPAIR")
    item = current_result(result_id)
    try:
        result = apply_verified_metadata_repair(item)
    except RepairError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, **result}
