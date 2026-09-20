from __future__ import annotations

from typing import Any, Mapping

from .db import latest_scan


NO_COMPLETE_SCAN = "NO_COMPLETE_SCAN"
STALE_RESULT = "STALE_RESULT"


class CurrentScanError(RuntimeError):
    """Raised when an action is not backed by the latest completed scan."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def require_latest_completed_scan() -> dict[str, Any]:
    """Return the latest scan only when it completed successfully."""
    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise CurrentScanError(
            NO_COMPLETE_SCAN,
            "A completed latest scan is required.",
        )
    return scan


def require_current_scan_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Require result to belong to the latest successfully completed scan."""
    scan = require_latest_completed_scan()
    if str(result.get("scan_id") or "") != str(scan.get("id") or ""):
        raise CurrentScanError(
            STALE_RESULT,
            "The result is not from the latest completed scan.",
        )
    return scan
