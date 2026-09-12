from __future__ import annotations

from fastapi import HTTPException

from ..db import latest_scan, result_by_id


def current_result(result_id: int) -> dict:
    """Return a result only when it belongs to the latest completed scan."""
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")

    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise HTTPException(status_code=409, detail="A completed latest scan is required.")
    if item.get("scan_id") != scan.get("id"):
        raise HTTPException(
            status_code=409,
            detail="This result is not from the latest completed scan. Refresh first.",
        )
    return item
