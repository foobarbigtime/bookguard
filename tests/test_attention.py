from jinja2 import Environment, FileSystemLoader

from app.static_assets import static_url

import app.attention as attention


def test_attention_snapshot_collects_only_intervention_states(monkeypatch):
    monkeypatch.setattr(
        attention,
        "_journal_rows",
        lambda table, id_column: (
            [{"id": 6, "status": "needs_review", "created_at": "2026-09-20T05:00:00Z", "completed_at": None, "error": "uncertain correction"}]
            if table == "hardlink_corrections"
            else [{"id": 7, "status": "running", "created_at": "2026-09-20T04:00:00Z", "completed_at": None, "error": None}]
        ),
    )
    monkeypatch.setattr(attention, "observe_attention_items", lambda limit: [])
    monkeypatch.setattr(attention, "_blocked_plan_items", lambda limit: [])
    monkeypatch.setattr(attention, "stale_execution_items", lambda limit: [])
    monkeypatch.setattr(attention, "_legacy_items", lambda limit: [])

    snapshot = attention.attention_snapshot()

    assert snapshot["total"] == 2
    assert snapshot["summary"] == {
        "legacy": 0,
        "hardlinkCorrections": 1,
        "hardlinkCleanups": 1,
        "recoveryPlans": 0,
        "runningExecutions": 0,
        "observe": 0,
    }
    statuses = {(item["kind"], item["status"]) for item in snapshot["items"]}
    assert ("hardlink_correction", "needs_review") in statuses
    assert ("hardlink_cleanup", "running") in statuses


def test_attention_snapshot_zero_state(monkeypatch):
    monkeypatch.setattr(attention, "_journal_rows", lambda table, id_column: [])
    monkeypatch.setattr(attention, "observe_attention_items", lambda limit: [])
    monkeypatch.setattr(attention, "_blocked_plan_items", lambda limit: [])
    monkeypatch.setattr(attention, "stale_execution_items", lambda limit: [])
    monkeypatch.setattr(attention, "_legacy_items", lambda limit: [])

    snapshot = attention.attention_snapshot()

    assert snapshot["total"] == 0
    assert snapshot["items"] == []


def test_home_renders_every_attention_item_with_its_guidance():
    from app.home import _workflow_groups

    env = Environment(loader=FileSystemLoader("templates"))
    env.globals["static_url"] = static_url
    attention = {
            "total": 1,
            "summary": {
                "acquisitions": 1,
                "admissions": 0,
                "hardlinkCorrections": 0,
                "hardlinkCleanups": 0,
                "coordinator": 0,
                "recoveryPlans": 0,
                "runningExecutions": 0,
                "observe": 0,
            },
            "generatedAt": "2026-09-20T11:30:00Z",
            "items": [
                {
                    "id": 1,
                    "kindLabel": "Acquisition",
                    "status": "cleanup_required",
                    "title": "Example Book",
                    "author": "Example Author",
                    "message": "Cleanup needs operator review.",
                    "guidance": {
                        "label": "Final cleanup needs recovery",
                        "why": "Temporary cleanup could not be proven complete.",
                        "nextStep": "Review the durable acquisition and recover cleanup.",
                    },
                    "updatedAt": "2026-09-20T11:29:00Z",
                    "detailHref": "/activity/acquisition/1",
                    "href": "/review/triage#acquisitionPanel",
                }
            ],
        }
    rendered = env.get_template("home.html").render(version="0.6.0", home=_minimal_home(_workflow_groups(attention)))

    assert "Example Book" in rendered
    assert "cleanup required" in rendered
    assert "Open guarded workflow" in rendered
    assert "Final cleanup needs recovery" in rendered
    assert "Why BookGuard stopped" in rendered
    assert "Next step" in rendered
    assert "Audit detail" in rendered
    assert "/activity/acquisition/1" in rendered


def _minimal_home(attention_groups):
    return {
        "health": {"level": "ok", "title": "BookGuard is healthy", "problems": []},
        "decisions": len(attention_groups),
        "library": {"checked": 0, "passed": 0, "verified": 0, "wrongFile": 0, "damaged": 0, "details": 0, "undecided": 0},
        "attention": attention_groups,
        "now": {"active": False, "label": "Nothing is running", "lastScan": None},
        "automation": {"mode": "manual", "sentence": "", "allowed": 0, "lastObserve": ""},
        "system": [],
        "recent": {"days": 7, "totals": {"added": 0, "quarantined": 0, "blocked": 0}, "events": []},
    }


def test_unfinished_legacy_acquisition_and_admission_stay_visible(tmp_path, monkeypatch):
    from app.config import settings
    from app.db import local_conn

    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    with local_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ebook_acquisitions (id INTEGER PRIMARY KEY, result_id INTEGER, status TEXT, error TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ebook_admissions (id INTEGER PRIMARY KEY, result_id INTEGER, status TEXT, error TEXT, created_at TEXT, updated_at TEXT)")
        conn.executemany(
            "INSERT INTO ebook_acquisitions(id, result_id, status, error, created_at, updated_at) VALUES (?, NULL, ?, ?, '2026-09-20T10:00:00Z', '2026-09-20T10:00:00Z')",
            [(1, "cleanup_required", "cleanup stopped"), (2, "finalized", None)],
        )
        conn.execute(
            "INSERT INTO ebook_admissions(id, result_id, status, error, created_at, updated_at) VALUES (4, NULL, 'registration_conflict', NULL, '2026-09-20T07:00:00Z', NULL)"
        )
        conn.commit()
    monkeypatch.setattr(attention, "_journal_rows", lambda table, id_column: [])
    monkeypatch.setattr(attention, "observe_attention_items", lambda limit: [])
    monkeypatch.setattr(attention, "_blocked_plan_items", lambda limit: [])
    monkeypatch.setattr(attention, "stale_execution_items", lambda limit: [])

    snapshot = attention.attention_snapshot()

    assert snapshot["summary"]["legacy"] == 2
    found = {(item["kind"], item["id"]): item for item in snapshot["items"]}
    assert set(found) == {("acquisition", 1), ("admission", 4)}
    assert found[("acquisition", 1)]["href"] == "/activity/acquisition/1"
    assert found[("admission", 4)]["detailHref"] == "/activity/admission/4"
    assert all("acquisitionPanel" not in item["href"] for item in snapshot["items"])


def test_no_attention_link_points_at_the_removed_triage_panel():
    from pathlib import Path

    for path in Path("app").rglob("*.py"):
        assert "acquisitionPanel" not in path.read_text(encoding="utf-8"), path


def _legacy_db(tmp_path, monkeypatch):
    import sqlite3

    from app.config import settings
    from app.db import local_db_path

    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "ebook_root", str(tmp_path / "books"))
    monkeypatch.setattr(settings, "ebook_bindery_prefix", "/data/media/books")
    (tmp_path / "config").mkdir()
    (tmp_path / "books").mkdir()
    monkeypatch.setattr(attention, "_journal_rows", lambda table, id_column: [])
    monkeypatch.setattr(attention, "observe_attention_items", lambda limit: [])
    monkeypatch.setattr(attention, "_blocked_plan_items", lambda limit: [])
    monkeypatch.setattr(attention, "stale_execution_items", lambda limit: [])
    conn = sqlite3.connect(local_db_path())
    conn.execute(
        "CREATE TABLE ebook_acquisitions (id INTEGER PRIMARY KEY, result_id INTEGER, status TEXT, "
        "queue_id INTEGER, staged_relative_path TEXT, admission_id INTEGER, error TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE ebook_admissions (id INTEGER PRIMARY KEY, result_id INTEGER, status TEXT, "
        "publication_method TEXT, failure_stage TEXT, stored_path TEXT, staged_sha256 TEXT, error TEXT, created_at TEXT, updated_at TEXT)"
    )
    return conn


def test_a_grab_bindery_refused_left_nothing_and_is_not_attention(tmp_path, monkeypatch):
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.executemany(
        "INSERT INTO ebook_acquisitions(id, result_id, status, queue_id, staged_relative_path, admission_id, error, created_at, updated_at) "
        "VALUES (?, NULL, 'failed', ?, NULL, NULL, 'Bindery grab failed: HTTP 409', 't', 't')",
        [(1, None), (2, 55)],
    )
    conn.commit()

    ids = {item["id"] for item in attention.attention_snapshot()["items"] if item["kind"] == "acquisition"}

    assert ids == {2}  # the one Bindery queued may have left a download behind


def test_a_failed_admission_counts_only_while_its_library_file_exists(tmp_path, monkeypatch):
    conn = _legacy_db(tmp_path, monkeypatch)
    stored = "/data/media/books/Stephen King/Sometimes They Come Back (1974)/Sometimes They Come Back - Stephen King.epub"
    import hashlib

    verified = hashlib.sha256(b"verified copy").hexdigest()
    conn.execute(
        "INSERT INTO ebook_admissions(id, result_id, status, publication_method, failure_stage, stored_path, staged_sha256, error, created_at, updated_at) "
        "VALUES (1, NULL, 'failed', NULL, NULL, ?, ?, '[Errno 22] Invalid argument', 't', 't')",
        (stored, verified),
    )
    conn.commit()

    def shown():
        return [item["id"] for item in attention.attention_snapshot()["items"] if item["kind"] == "admission"]

    assert shown() == []  # no file at the path

    local = tmp_path / "books" / "Stephen King" / "Sometimes They Come Back (1974)" / "Sometimes They Come Back - Stephen King.epub"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"a later copy from Bindery")
    assert shown() == []  # a different file: not the copy the admission verified

    local.write_bytes(b"verified copy")
    assert shown() == [1]  # the admission's own bytes are there: check it


def test_home_offers_no_automatic_mode():
    from pathlib import Path

    assert "'Automatic'" not in Path("templates/home.html").read_text(encoding="utf-8")
