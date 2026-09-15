from fastapi import APIRouter, HTTPException
from pydantic import Field

from ..actions import ActionError
from ..hardlink_conflicts import (
    conflict_preview, correct_hardlink_conflict, hardlink_conflicts, reconcile_correction,
)
from ..hardlink_cleanup import cleanup_preview, cleanup_staging_alias, reconcile_alias_cleanup
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


@router.get("/history/{correction_id}/cleanup-preview")
def api_hardlink_cleanup_preview(correction_id: int):
    return cleanup_preview(correction_id)


@router.post("/history/{correction_id}/cleanup")
def api_hardlink_cleanup(correction_id: int, payload: CorrectionRequest):
    require_confirmation(payload, "REMOVE_UNREGISTERED_STAGING_LINK")
    try:
        return cleanup_staging_alias(correction_id, payload.token)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/history/{correction_id}/reconcile-cleanup")
def api_hardlink_reconcile_cleanup(correction_id: int, payload: ConfirmationRequest):
    require_confirmation(payload, "RECONCILE_STAGING_LINK_CLEANUP")
    try:
        return reconcile_alias_cleanup(correction_id)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{file_id}/correct")
def api_hardlink_correct(file_id: int, payload: CorrectionRequest):
    require_confirmation(payload, "REMOVE_WRONG_EBOOK_ASSOCIATION")
    try:
        return correct_hardlink_conflict(file_id, payload.token)
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
