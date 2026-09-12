from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..actions import ActionError, detach_missing, missing_detach_preview
from ..db import latest_results, latest_scan, result_by_id
from ..scanner import start_scan
from ..services.dashboard import latest_missing_cleanup
from ..triage import (
    TRIAGE_CLASSES,
    clear_keep_decision,
    save_keep_decision,
    triage_action_preview,
    triage_detach,
    triage_quarantine,
)


router = APIRouter(prefix="/api", tags=["triage"])


def _triage_item(result_id: int) -> dict:
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    if item.get("classification") not in TRIAGE_CLASSES:
        raise HTTPException(status_code=400, detail="Only REVIEW and REJECT results can use triage.")
    return item


@router.post("/triage/{result_id}/keep")
async def api_triage_keep(result_id: int, request: Request):
    item = _triage_item(result_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("confirm") != "KEEP":
        raise HTTPException(status_code=400, detail="Explicit KEEP confirmation is required.")
    try:
        decision_id = save_keep_decision(item)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "ok": True,
        "decision_id": decision_id,
        "message": "Triage decision saved. Bindery and media files were not changed.",
    }

@router.post("/triage/keep-selected")
async def api_triage_keep_selected(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("confirm") != "KEEP":
        raise HTTPException(status_code=400, detail="Explicit KEEP confirmation is required.")
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="Select at least one triage result.")
    if len(ids) > 5000:
        raise HTTPException(status_code=400, detail="Too many selected results.")

    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise HTTPException(status_code=409, detail="A completed latest scan is required.")

    items = []
    for raw_id in ids:
        try:
            result_id = int(raw_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid result id in selection.")
        item = _triage_item(result_id)
        if item.get("scan_id") != scan["id"]:
            raise HTTPException(status_code=409, detail="The scan changed. Refresh triage before saving selections.")
        items.append(item)

    decision_ids = []
    try:
        for item in items:
            decision_ids.append(save_keep_decision(item))
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "saved": len(decision_ids), "decision_ids": decision_ids}


@router.post("/triage/{result_id}/reopen")
async def api_triage_reopen(result_id: int, request: Request):
    item = _triage_item(result_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("confirm") != "REOPEN":
        raise HTTPException(status_code=400, detail="Explicit REOPEN confirmation is required.")
    try:
        removed = clear_keep_decision(item)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "removed": removed}


@router.get("/triage/{result_id}/action-preview")
def api_triage_action_preview(result_id: int, action: str):
    item = _triage_item(result_id)
    try:
        preview = triage_action_preview(item, action)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"result_id": result_id, **preview}


@router.post("/triage/{result_id}/detach")
async def api_triage_detach(result_id: int, request: Request):
    item = _triage_item(result_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("confirm") != "DETACH":
        raise HTTPException(status_code=400, detail="Explicit DETACH confirmation is required.")
    try:
        cleanup_id = triage_detach(item)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "ok": True,
        "cleanup_id": cleanup_id,
        "message": "Exact Bindery association detached. The physical file was left in place.",
    }


@router.post("/triage/{result_id}/quarantine")
async def api_triage_quarantine(result_id: int, request: Request):
    item = _triage_item(result_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("confirm") != "QUARANTINE":
        raise HTTPException(status_code=400, detail="Explicit QUARANTINE confirmation is required.")
    try:
        cleanup_id, destination = triage_quarantine(item)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "ok": True,
        "cleanup_id": cleanup_id,
        "destination": destination,
        "message": f"Item detached from Bindery and moved to {destination}",
    }


@router.get("/results/{result_id}/missing-detach-preview")
def api_missing_detach_preview(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    try:
        return {"result_id": result_id, **missing_detach_preview(item)}
    except ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/missing/preview")
def api_missing_preview():
    return latest_missing_cleanup()


@router.post("/results/{result_id}/detach-missing")
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


@router.post("/missing/detach-all")
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
