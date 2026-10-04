from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..observe import observe_snapshot, run_observe_cycle
from .models import ConfirmationRequest, require_confirmation


router = APIRouter(prefix="/api/automatic", tags=["automatic maintenance"])


@router.get("/observe")
def api_automatic_observe(limit: int = 100):
    """Return durable Observe Mode decisions without running a new cycle."""
    try:
        return observe_snapshot(limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/observe/run")
def api_automatic_observe_run(payload: ConfirmationRequest):
    """Record what Automatic Mode would do without invoking mutation workflows."""
    require_confirmation(payload, "RUN_OBSERVE_MODE")
    try:
        result = run_observe_cycle()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not result.get("enabled"):
        raise HTTPException(status_code=409, detail=str(result.get("message") or "Observe Mode is disabled."))
    return result
