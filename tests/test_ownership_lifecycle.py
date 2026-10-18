from unittest.mock import patch

from app import scanner
from app.config import settings
from app.db import create_scan, init_local_db, local_conn


def _setup(tmp_path, monkeypatch, scan_id):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    init_local_db()
    create_scan(scan_id, 0)


def _scan_status(scan_id):
    with local_conn() as conn:
        return conn.execute(
            "SELECT status FROM scans WHERE id=?",
            (scan_id,),
        ).fetchone()["status"]


def test_completed_scan_triggers_audit(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "complete")

    with patch.object(scanner, "_run_ownership_audit") as audit:
        scanner.run_scan("complete", [])

    audit.assert_called_once_with("complete")
    assert _scan_status("complete") == "complete"


def test_audit_exception_preserves_completed_scan(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "audit-error")

    with patch.object(
        scanner,
        "_run_ownership_audit",
        side_effect=RuntimeError("Unexpected audit error"),
    ) as audit:
        scanner.run_scan("audit-error", [])

    audit.assert_called_once_with("audit-error")
    assert _scan_status("audit-error") == "complete"


def test_immediate_stop_skips_audit(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "stopped")

    scanner._immediate_stop_event.set()

    with patch.object(scanner, "_run_ownership_audit") as audit:
        scanner.run_scan("stopped", [{"book_id": 1}])

    audit.assert_not_called()
    assert _scan_status("stopped") == "stopped"


def test_safe_cancel_skips_audit(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "cancelled")

    scanner._safe_cancel_event.set()

    with patch.object(scanner, "_run_ownership_audit") as audit:
        scanner.run_scan("cancelled", [{"book_id": 1}])

    audit.assert_not_called()
    assert _scan_status("cancelled") == "cancelled"


def test_failed_scan_skips_audit(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "failed")

    with (
        patch.object(
            scanner,
            "_raise_if_immediate_stop",
            side_effect=RuntimeError("Simulated scan failure"),
        ),
        patch.object(scanner, "_run_ownership_audit") as audit,
    ):
        scanner.run_scan("failed", [{"book_id": 1}])

    audit.assert_not_called()
    assert _scan_status("failed") == "failed"
