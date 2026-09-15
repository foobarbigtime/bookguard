from fastapi import APIRouter, HTTPException
from pydantic import Field

from ..actions import ActionError
from ..hardlink_conflicts import (
    conflict_preview, correct_hardlink_conflict, hardlink_conflicts, reconcile_correction,
)
from .models import ConfirmationRequest, require_confirmation


router = APIRouter(prefix="/api/hardlink-conflicts", tags=["hard-link conflicts"])


class CorrectionRequest(ConfirmationRequest):
    token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


@router.get("")
def api_hardlink_conflicts():
    try:
        return hardlink_conflicts()
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{file_id}/preview")
def api_hardlink_preview(file_id: int):
    return conflict_preview(file_id)


@router.post("/history/{correction_id}/reconcile")
def api_hardlink_reconcile(correction_id: int, payload: ConfirmationRequest):
    require_confirmation(payload, "RECONCILE_HARDLINK_CORRECTION")
    try:
        return reconcile_correction(correction_id)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{file_id}/correct")
def api_hardlink_correct(file_id: int, payload: CorrectionRequest):
    require_confirmation(payload, "REMOVE_WRONG_EBOOK_ASSOCIATION")
    try:
        return correct_hardlink_conflict(file_id, payload.token)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
