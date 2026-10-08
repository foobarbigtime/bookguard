from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..actions import ActionError, detach_missing, missing_detach_preview, resolve_bindery_api_key
from ..bindery_client import BinderyClient
from ..config import settings
from ..catalogue_move import (
    CatalogueMoveError,
    list_moves,
    move_preview,
    move_to_correct_book,
    reconcile_move,
)
from ..db import latest_results, latest_scan, result_by_id
from ..put_back import put_back
from ..replace import triage_replace
from ..unmatched import attach as attach_unmatched, check_status, start_check
from ..duplicates import fix_status, start_fix
from ..library_check import start_allowed
from ..scanner import start_scan
from ..services.dashboard import latest_missing_cleanup
from ..verifier import verify_result
from .models import ConfirmationRequest, require_confirmation
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


@router.post("/unmatched/check", status_code=202)
def api_unmatched_check(payload: ConfirmationRequest):
    """Check Bindery's unmatched files in the background (read-only)."""
    require_confirmation(payload, "CHECK_UNMATCHED")
    started = start_check()
    return {"ok": True, "started": started, "status": check_status(),
            "message": "Checking Bindery's unmatched files." if started else "A check is already running."}


@router.post("/unmatched/{row_id}/attach")
def api_unmatched_attach(row_id: int, payload: ConfirmationRequest):
    """Adopt one proven unmatched file to its book through Bindery."""
    require_confirmation(payload, "ATTACH")
    try:
        message = attach_unmatched(row_id)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "message": message}


@router.post("/duplicates/fix", status_code=202)
def api_duplicate_fix(payload: ConfirmationRequest):
    """Fix every proven duplicate now, in the background (the same as the automatic run)."""
    require_confirmation(payload, "FIX_DUPLICATES")
    if not settings.allow_actions:
        raise HTTPException(status_code=409, detail="Actions are disabled. Turn them on in Settings to fix duplicates.")
    started = start_fix()
    return {"ok": True, "started": started, "status": fix_status(),
            "message": "Fixing duplicates in Bindery." if started else "Duplicates are already being fixed."}


@router.post("/triage/{result_id}/replace")
def api_triage_replace(result_id: int, payload: ConfirmationRequest):
    """Quarantine one file and have Bindery blocklist its release and fetch a new copy."""
    require_confirmation(payload, "REPLACE")
    item = _triage_item(result_id)
    try:
        cleanup_id, message = triage_replace(item)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "cleanup_id": cleanup_id, "message": message}


@router.post("/quarantine/{cleanup_id}/put-back")
def api_put_back(cleanup_id: int, payload: ConfirmationRequest):
    """Move one quarantined file back to its original path."""
    require_confirmation(payload, "PUT_BACK")
    try:
        put_back_id, message = put_back(cleanup_id)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "cleanup_id": put_back_id, "message": message}


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
    if not start_allowed():
        raise HTTPException(status_code=409, detail="Check library is running. Wait for it to finish before MISSING cleanup.")
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
    follow_up = (
        "A validation scan was started." if validation_scan_id else
        "A validation scan could not start because another library task is running; "
        "run Check library when it finishes."
    )
    return {
        "ok": True,
        "detached": len(cleanup_ids),
        "cleanup_ids": cleanup_ids,
        "scan_id": validation_scan_id,
        "message": (
            f"Detached {len(cleanup_ids)} stale Bindery association(s). "
            f"No physical files were deleted or moved. {follow_up}"
        ),
    }


@router.get("/triage/{result_id}/move-preview")
def api_triage_move_preview(result_id: int):
    """Read-only: can this file be moved to the Bindery book it really is?"""
    item = _triage_item(result_id)
    try:
        client = BinderyClient(api_key=resolve_bindery_api_key())
        return move_preview(item, verify_result(item), client)
    except (ActionError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/triage/{result_id}/move-to-correct-book")
async def api_triage_move_to_correct_book(result_id: int, request: Request):
    item = _triage_item(result_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    confirm = payload.get("confirm") if isinstance(payload, dict) else None
    try:
        client = BinderyClient(api_key=resolve_bindery_api_key())
        move = move_to_correct_book(item, str(confirm or ""), verify=verify_result, client=client)
    except (ActionError, CatalogueMoveError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    messages = {
        "confirmed": "Bindery moved the file to the right book; its fingerprint matches the verified file.",
        "pending": "Bindery accepted the move but has not finished; check again with reconcile.",
        "fingerprint_mismatch": "Bindery tracks the new path, but the file there does not match the verified file.",
    }
    return {"ok": move["status"] == "confirmed", "move": move, "message": messages.get(move["status"], move["status"])}


@router.post("/catalogue-moves/{move_id}/reconcile")
def api_catalogue_move_reconcile(move_id: int):
    """Read-only re-check of a requested move; never asks Bindery again."""
    try:
        return reconcile_move(move_id)
    except CatalogueMoveError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/catalogue-moves")
def api_catalogue_moves():
    return {"moves": list_moves()}
