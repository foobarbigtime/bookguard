"""Read-only ownership audit summary regression tests."""

import pytest

from app import db


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "config_dir", str(tmp_path))
    db.init_local_db()
    return db


@pytest.mark.parametrize(
    ("state", "count", "expected"),
    [
        ("running", None, None),
        ("failed", None, None),
        ("completed", 0, 0),
        ("completed", 19, 19),
    ],
)
def test_ownership_summary_states(audit_db, state, count, expected):
    scan_id = f"summary-{state}-{count}"
    audit_db.create_scan(scan_id, 0)

    with audit_db.local_conn() as conn:
        conn.execute(
            """
            INSERT INTO ownership_audits
                (scan_id, status, started_at, conflict_count, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                state,
                "2026-10-18T12:00:00+00:00",
                count,
                "Test failure" if state == "failed" else None,
            ),
        )
        conn.commit()

    result = audit_db.ownership_audit_summary(scan_id)

    assert result["status"] == state
    assert result["conflict_count"] == expected

    if state == "failed":
        assert result["error"] == "Test failure"


def test_missing_audit_is_not_clean(audit_db):
    audit_db.create_scan("not-yet-audited", 0)

    result = audit_db.ownership_audit_summary("not-yet-audited")

    assert result["status"] == "not_audited"
    assert result["conflict_count"] is None


def test_summary_does_not_modify_database(audit_db):
    audit_db.create_scan("read-only", 0)

    audit_db.ownership_audit_summary("read-only")

    with audit_db.local_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ownership_audits"
        ).fetchone()[0] == 0
