import pytest

import app.scan_guard as scan_guard
from app.scan_guard import CurrentScanError, NO_COMPLETE_SCAN, STALE_RESULT


def test_require_latest_completed_scan_rejects_missing_scan(monkeypatch):
    monkeypatch.setattr(scan_guard, "latest_scan", lambda: None)

    with pytest.raises(CurrentScanError) as exc_info:
        scan_guard.require_latest_completed_scan()

    assert exc_info.value.code == NO_COMPLETE_SCAN


def test_require_latest_completed_scan_rejects_incomplete_scan(monkeypatch):
    monkeypatch.setattr(scan_guard, "latest_scan", lambda: {"id": "scan-1", "status": "running"})

    with pytest.raises(CurrentScanError) as exc_info:
        scan_guard.require_latest_completed_scan()

    assert exc_info.value.code == NO_COMPLETE_SCAN


def test_require_current_scan_result_rejects_stale_result(monkeypatch):
    monkeypatch.setattr(
        scan_guard,
        "latest_scan",
        lambda: {"id": "scan-current", "status": "complete"},
    )

    with pytest.raises(CurrentScanError) as exc_info:
        scan_guard.require_current_scan_result({"scan_id": "scan-old"})

    assert exc_info.value.code == STALE_RESULT


def test_require_current_scan_result_accepts_latest_completed_scan(monkeypatch):
    expected = {"id": "scan-current", "status": "complete"}
    monkeypatch.setattr(scan_guard, "latest_scan", lambda: expected)

    scan = scan_guard.require_current_scan_result({"scan_id": "scan-current"})

    assert scan is expected
