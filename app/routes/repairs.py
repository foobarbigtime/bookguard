from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..db import result_by_id
from ..repair import RepairError, apply_metadata_repair, build_repair_preview, undo_metadata_repair


router = APIRouter(prefix="/api", tags=["repairs"])



@router.get("/results/{result_id}/repair-preview")
def api_repair_preview(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    try:
        preview = build_repair_preview(item)
    except RepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"result_id": result_id, "mode": settings.metadata_repair_mode, **preview}


@router.post("/results/{result_id}/repair")
def api_repair(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    try:
        return {"ok": True, **apply_metadata_repair(item)}
    except RepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/repairs/{repair_id}/undo")
def api_undo_repair(repair_id: int):
    try:
        return undo_metadata_repair(repair_id)
    except RepairError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
