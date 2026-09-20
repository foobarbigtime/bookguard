import app.attention as attention


def _result(result_id):
    return {"id": result_id, "title": f"Book {result_id}", "author": "Example Author"}


def test_attention_snapshot_collects_only_intervention_states(monkeypatch):
    monkeypatch.setattr(
        attention,
        "recent_ebook_acquisitions",
        lambda limit: [
            {"id": 1, "result_id": 11, "status": "cleanup_required", "error": "cleanup stopped", "updated_at": "2026-09-20T10:00:00Z"},
            {"id": 2, "result_id": 12, "status": "finalized", "error": None, "updated_at": "2026-09-20T09:00:00Z"},
            {"id": 3, "result_id": 13, "status": "review_required", "error": "verification needs review", "updated_at": "2026-09-20T08:00:00Z"},
        ],
    )
    monkeypatch.setattr(
        attention,
        "recent_ebook_admissions",
        lambda limit: [
            {"id": 4, "result_id": 14, "status": "registration_conflict", "error": None, "updated_at": "2026-09-20T07:00:00Z"},
            {"id": 5, "result_id": 15, "status": "registered", "error": None, "updated_at": "2026-09-20T06:00:00Z"},
        ],
    )
    monkeypatch.setattr(attention, "result_by_id", _result)
    monkeypatch.setattr(
        attention,
        "_journal_rows",
        lambda table, id_column: (
            [{"id": 6, "status": "needs_review", "created_at": "2026-09-20T05:00:00Z", "completed_at": None, "error": "uncertain correction"}]
            if table == "hardlink_corrections"
            else [{"id": 7, "status": "running", "created_at": "2026-09-20T04:00:00Z", "completed_at": None, "error": None}]
        ),
    )
    monkeypatch.setattr(
        attention,
        "acquisition_coordinator_status",
        lambda: {"state": "attention_required", "lastError": "explicit recovery required", "lastRunAt": "2026-09-20T03:00:00Z"},
    )

    snapshot = attention.attention_snapshot()

    assert snapshot["total"] == 6
    assert snapshot["summary"] == {
        "acquisitions": 2,
        "admissions": 1,
        "hardlinkCorrections": 1,
        "hardlinkCleanups": 1,
        "coordinator": 1,
    }
    statuses = {(item["kind"], item["status"]) for item in snapshot["items"]}
    assert ("acquisition", "cleanup_required") in statuses
    assert ("acquisition", "review_required") in statuses
    assert ("admission", "registration_conflict") in statuses
    assert ("hardlink_correction", "needs_review") in statuses
    assert ("hardlink_cleanup", "running") in statuses
    assert ("coordinator", "attention_required") in statuses
    assert ("acquisition", "finalized") not in statuses
    assert ("admission", "registered") not in statuses


def test_attention_snapshot_zero_state(monkeypatch):
    monkeypatch.setattr(attention, "recent_ebook_acquisitions", lambda limit: [])
    monkeypatch.setattr(attention, "recent_ebook_admissions", lambda limit: [])
    monkeypatch.setattr(attention, "_journal_rows", lambda table, id_column: [])
    monkeypatch.setattr(attention, "acquisition_coordinator_status", lambda: {"state": "idle"})

    snapshot = attention.attention_snapshot()

    assert snapshot["total"] == 0
    assert snapshot["items"] == []
