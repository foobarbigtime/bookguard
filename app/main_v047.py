from __future__ import annotations

from fastapi import HTTPException, Request

from .main import app
from .db import latest_scan, result_by_id
from .repair import RepairError
from .verifier import (
    apply_verified_metadata_repair,
    init_verification_db,
    start_verification_job,
    test_tika,
    verification_for_result,
    verification_job_status,
    verification_summary,
    verified_repair_preview,
    verify_result,
)


app.version = "0.4.7"


@app.on_event("startup")
def startup_verification() -> None:
    init_verification_db()


def _current_result(result_id: int) -> dict:
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise HTTPException(status_code=409, detail="A completed latest scan is required.")
    if item.get("scan_id") != scan.get("id"):
        raise HTTPException(status_code=409, detail="This result is not from the latest completed scan. Refresh first.")
    return item


@app.get("/api/verification/status")
def api_verification_status():
    return {
        "job": verification_job_status(),
        "summary": verification_summary(),
    }


@app.post("/api/verification/start")
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


@app.get("/api/verification/tika-test")
def api_verification_tika_test():
    return test_tika()


@app.get("/api/verification/{result_id}")
def api_verification_get(result_id: int):
    item = _current_result(result_id)
    verification = verification_for_result(item)
    if not verification:
        return {"result_id": result_id, "verified": False}
    return {"result_id": result_id, "verified": True, "verification": verification}


@app.post("/api/verification/{result_id}/run")
def api_verification_run(result_id: int):
    item = _current_result(result_id)
    try:
        verification = verify_result(item, force=True)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "verification": verification}


@app.get("/api/verification/{result_id}/repair-preview")
def api_verified_repair_preview(result_id: int):
    item = _current_result(result_id)
    verification = verification_for_result(item)
    try:
        preview = verified_repair_preview(item, verification)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "result_id": result_id,
        "mode": app.version and __import__("app.config", fromlist=["settings"]).settings.metadata_repair_mode,
        **preview,
    }


@app.post("/api/verification/{result_id}/repair")
def api_verified_repair(result_id: int):
    item = _current_result(result_id)
    try:
        result = apply_verified_metadata_repair(item)
    except RepairError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, **result}
