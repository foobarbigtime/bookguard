from unittest.mock import patch

from app import scanner
from app.config import settings
from app.db import (
    create_scan,
    init_local_db,
    local_conn,
)


def _setup(tmp_path, monkeypatch, scan_id):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    init_local_db()
    create_scan(scan_id, 0)


def _audit(scan_id):
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ownership_audits WHERE scan_id=?",
            (scan_id,),
        ).fetchone()
    return dict(row) if row else None


def test_successful_ownership_audit(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "success")

    with patch(
        "app.ownership_audit.audit_ownership", return_value=[]
    ):
        scanner._run_ownership_audit("success")

    audit = _audit("success")
    assert audit["status"] == "completed"
    assert audit["conflict_count"] == 0


def test_detector_failure_is_recorded(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "detector-failed")

    with patch(
        "app.ownership_audit.audit_ownership",
        side_effect=RuntimeError("Bindery unavailable"),
    ):
        scanner._run_ownership_audit("detector-failed")

    audit = _audit("detector-failed")
    assert audit["status"] == "failed"
    assert audit["conflict_count"] is None
    assert "Bindery unavailable" in audit["error"]


def test_start_failure_does_not_escape(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "start-failed")

    with patch(
        "app.db.start_ownership_audit",
        side_effect=RuntimeError("Database unavailable"),
    ):
        scanner._run_ownership_audit("start-failed")

    assert _audit("start-failed") is None


def test_persistence_failure_is_recorded(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "save-failed")

    with (
        patch("app.ownership_audit.audit_ownership", return_value=[]),
        patch(
            "app.db.complete_ownership_audit",
            side_effect=RuntimeError("Write failed"),
        ),
    ):
        scanner._run_ownership_audit("save-failed")

    audit = _audit("save-failed")
    assert audit["status"] == "failed"
    assert audit["conflict_count"] is None
    assert "Write failed" in audit["error"]
