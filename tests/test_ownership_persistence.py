import sqlite3

import pytest

from app.config import settings
from app.db import (
    complete_ownership_audit,
    create_scan,
    fail_ownership_audit,
    init_local_db,
    local_conn,
    start_ownership_audit,
)


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    init_local_db()
    return tmp_path


@pytest.fixture
def conflict():
    return {
        "parent_file_id": 1,
        "parent_book_id": 10,
        "parent_title": "Parent Collection",
        "parent_path": "/audiobooks/Parent",
        "child_file_id": 2,
        "child_book_id": 20,
        "child_title": "Child Book",
        "child_path": "/audiobooks/Parent/Child/Track.mp3",
    }


def test_completed_audit_persists_conflicts(audit_db, conflict):
    create_scan("scan-one", 0)
    start_ownership_audit("scan-one")
    complete_ownership_audit("scan-one", [conflict])

    with local_conn() as conn:
        audit = conn.execute(
            "SELECT * FROM ownership_audits WHERE scan_id=?",
            ("scan-one",),
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM ownership_conflicts WHERE scan_id=?",
            ("scan-one",),
        ).fetchall()

    assert audit["status"] == "completed"
    assert audit["conflict_count"] == 1
    assert audit["finished_at"] is not None
    assert len(rows) == 1
    assert rows[0]["child_file_id"] == 2


def test_zero_conflicts_is_success(audit_db):
    create_scan("scan-zero", 0)
    start_ownership_audit("scan-zero")
    complete_ownership_audit("scan-zero", [])

    with local_conn() as conn:
        audit = conn.execute(
            "SELECT * FROM ownership_audits WHERE scan_id=?",
            ("scan-zero",),
        ).fetchone()

    assert audit["status"] == "completed"
    assert audit["conflict_count"] == 0
    assert audit["error"] is None


def test_duplicate_conflict_rolls_back(audit_db, conflict):
    create_scan("scan-rollback", 0)
    start_ownership_audit("scan-rollback")

    with pytest.raises(sqlite3.IntegrityError):
        complete_ownership_audit(
            "scan-rollback", [conflict, conflict]
        )

    with local_conn() as conn:
        audit = conn.execute(
            "SELECT * FROM ownership_audits WHERE scan_id=?",
            ("scan-rollback",),
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM ownership_conflicts WHERE scan_id=?",
            ("scan-rollback",),
        ).fetchone()[0]

    assert audit["status"] == "running"
    assert audit["conflict_count"] is None
    assert count == 0


def test_failed_audit_never_reports_zero(audit_db):
    create_scan("scan-failed", 0)
    start_ownership_audit("scan-failed")
    fail_ownership_audit("scan-failed", "Bindery unavailable")

    with local_conn() as conn:
        audit = conn.execute(
            "SELECT * FROM ownership_audits WHERE scan_id=?",
            ("scan-failed",),
        ).fetchone()

    assert audit["status"] == "failed"
    assert audit["conflict_count"] is None
    assert audit["error"] == "Bindery unavailable"


def test_completed_audit_cannot_be_overwritten(audit_db):
    create_scan("scan-locked", 0)
    start_ownership_audit("scan-locked")
    complete_ownership_audit("scan-locked", [])

    with pytest.raises(ValueError):
        fail_ownership_audit("scan-locked", "Unexpected error")

    with pytest.raises(ValueError):
        complete_ownership_audit("scan-locked", [])


def test_orphan_audit_rejected(audit_db):
    with pytest.raises(sqlite3.IntegrityError):
        start_ownership_audit("nonexistent-scan")


def test_foreign_keys_enabled(audit_db):
    with local_conn() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_initialization_is_repeatable(audit_db):
    init_local_db()
    with local_conn() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"ownership_audits", "ownership_conflicts"} <= tables


def test_retrieve_completed_ownership_conflicts(audit_db, conflict):
    from app.db import ownership_audit_conflicts

    create_scan("retrieve-completed", 0)
    start_ownership_audit("retrieve-completed")
    complete_ownership_audit("retrieve-completed", [conflict])

    rows = ownership_audit_conflicts("retrieve-completed")

    assert len(rows) == 1
    assert rows[0] == conflict
    assert ownership_audit_conflicts("missing-scan") == []


@pytest.mark.parametrize("status", ["running", "failed"])
def test_retrieval_excludes_incomplete_audits(
    audit_db, conflict, status
):
    from app.db import ownership_audit_conflicts

    scan_id = f"retrieve-{status}"
    create_scan(scan_id, 0)
    start_ownership_audit(scan_id)

    if status == "failed":
        fail_ownership_audit(scan_id, "Test failure")

    # Deliberately insert an abnormal record to verify
    # that the reader excludes incomplete audits.
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO ownership_conflicts (
                scan_id,
                parent_file_id, parent_book_id,
                parent_title, parent_path,
                child_file_id, child_book_id,
                child_title, child_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                conflict["parent_file_id"],
                conflict["parent_book_id"],
                conflict["parent_title"],
                conflict["parent_path"],
                conflict["child_file_id"],
                conflict["child_book_id"],
                conflict["child_title"],
                conflict["child_path"],
            ),
        )
        conn.commit()

    assert ownership_audit_conflicts(scan_id) == []


def test_ownership_conflicts_are_isolated_by_scan(audit_db, conflict):
    from app.db import ownership_audit_conflicts

    for scan_id, child_id in [
        ("retrieve-first", 101),
        ("retrieve-second", 202),
    ]:
        create_scan(scan_id, 0)
        start_ownership_audit(scan_id)
        complete_ownership_audit(
            scan_id,
            [{**conflict, "child_file_id": child_id}],
        )

    first = ownership_audit_conflicts("retrieve-first")
    second = ownership_audit_conflicts("retrieve-second")

    assert [row["child_file_id"] for row in first] == [101]
    assert [row["child_file_id"] for row in second] == [202]
