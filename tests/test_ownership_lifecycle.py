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


def test_restart_recovers_abandoned_ownership_audit(tmp_path, monkeypatch):
    from app.db import (
        recover_interrupted_ownership_audits,
        start_ownership_audit,
    )

    _setup(tmp_path, monkeypatch, "interrupted")
    create_scan("completed-history", 0)
    start_ownership_audit("interrupted")
    start_ownership_audit("completed-history")

    with local_conn() as conn:
        conn.execute(
            """
            UPDATE ownership_audits
            SET status='completed', finished_at='2026-01-01T00:00:00',
                conflict_count=0
            WHERE scan_id='completed-history'
            """
        )
        conn.commit()

    recover_interrupted_ownership_audits()

    with local_conn() as conn:
        interrupted = conn.execute(
            "SELECT * FROM ownership_audits WHERE scan_id='interrupted'"
        ).fetchone()
        completed = conn.execute(
            "SELECT * FROM ownership_audits WHERE scan_id='completed-history'"
        ).fetchone()

    assert interrupted["status"] == "failed"
    assert interrupted["finished_at"] is not None
    assert interrupted["conflict_count"] is None
    assert "interrupt" in interrupted["error"].lower()
    assert completed["status"] == "completed"
    assert completed["conflict_count"] == 0
    assert completed["finished_at"] == "2026-01-01T00:00:00"

    recover_interrupted_ownership_audits()

    with local_conn() as conn:
        recovered_again = conn.execute(
            "SELECT * FROM ownership_audits WHERE scan_id='interrupted'"
        ).fetchone()

    assert dict(recovered_again) == dict(interrupted)
    assert _scan_status("interrupted") == "running"


def test_application_startup_recovers_interrupted_audit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import start_ownership_audit

    _setup(tmp_path, monkeypatch, "startup-interrupted")
    start_ownership_audit("startup-interrupted")

    monkeypatch.setattr(settings, "scan_on_start", False)
    monkeypatch.setattr(main, "record_app_start", lambda: None)
    monkeypatch.setattr(main, "init_triage_db", lambda: None)
    monkeypatch.setattr(main, "init_verification_db", lambda: None)
    monkeypatch.setattr(main, "init_import_watch_db", lambda: None)
    monkeypatch.setattr(main, "init_scheduler_db", lambda: None)
    monkeypatch.setattr(main.import_watcher, "start", lambda: None)
    monkeypatch.setattr(main.import_watcher, "stop", lambda: None)
    monkeypatch.setattr(main.scheduler, "start", lambda: None)
    monkeypatch.setattr(main.scheduler, "stop", lambda: None)
    monkeypatch.setattr(
        main,
        "load_persisted_settings",
        lambda: {},
    )

    with TestClient(main.app):
        with local_conn() as conn:
            audit = conn.execute(
                """
                SELECT status, finished_at, conflict_count, error
                FROM ownership_audits
                WHERE scan_id='startup-interrupted'
                """
            ).fetchone()

        assert audit["status"] == "failed"
        assert audit["finished_at"] is not None
        assert audit["conflict_count"] is None
        assert "interrupt" in audit["error"].lower()
