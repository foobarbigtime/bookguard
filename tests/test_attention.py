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

    snapshot = attention.attention_snapshot()

    assert snapshot["total"] == 2
    assert snapshot["summary"] == {
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
