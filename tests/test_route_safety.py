import pytest
from fastapi import HTTPException

import app.scan_guard as scan_guard
from app.routes import dependencies
from app.routes.models import ConfirmationRequest, require_confirmation


def test_current_result_rejects_stale_scan(monkeypatch):
    monkeypatch.setattr(
        dependencies,
        "result_by_id",
        lambda result_id: {"id": result_id, "scan_id": "old-scan"},
    )
    monkeypatch.setattr(
        scan_guard,
        "latest_scan",
        lambda: {"id": "current-scan", "status": "complete"},
    )

    with pytest.raises(HTTPException) as exc_info:
        dependencies.current_result(42)

    assert exc_info.value.status_code == 409
    assert "latest completed scan" in exc_info.value.detail


def test_current_result_accepts_latest_completed_scan(monkeypatch):
    expected = {"id": 42, "scan_id": "current-scan"}
    monkeypatch.setattr(dependencies, "result_by_id", lambda result_id: expected)
    monkeypatch.setattr(
        scan_guard,
        "latest_scan",
        lambda: {"id": "current-scan", "status": "complete"},
    )

    assert dependencies.current_result(42) is expected


def test_confirmation_must_match_exactly():
    require_confirmation(ConfirmationRequest(confirm="REPAIR"), "REPAIR")

    with pytest.raises(HTTPException) as exc_info:
        require_confirmation(ConfirmationRequest(confirm="repair"), "REPAIR")

    assert exc_info.value.status_code == 400
    assert "Explicit REPAIR" in exc_info.value.detail
