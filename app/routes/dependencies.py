from __future__ import annotations

from fastapi import HTTPException

from ..db import result_by_id
from ..scan_guard import CurrentScanError, NO_COMPLETE_SCAN, require_current_scan_result


def current_result(result_id: int) -> dict:
    """Return a result only when it belongs to the latest completed scan."""
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")

    try:
        require_current_scan_result(item)
    except CurrentScanError as exc:
        detail = (
            "A completed latest scan is required."
            if exc.code == NO_COMPLETE_SCAN
            else "This result is not from the latest completed scan. Refresh first."
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    return item
