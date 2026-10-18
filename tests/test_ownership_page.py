from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import pages


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app)


@pytest.fixture
def conflict():
    return {
        "parent_file_id": 1,
        "parent_book_id": 10,
        "parent_title": "<script>alert(1)</script>",
        "parent_path": "/audiobooks/<unsafe>/Parent",
        "child_file_id": 2,
        "child_book_id": 20,
        "child_title": "Different Book",
        "child_path": "/audiobooks/Parent/Child.mp3",
    }


@pytest.mark.parametrize(
    "status,count,expected",
    [
        ("completed", 0, "No ownership conflicts detected"),
        ("running", None, "Ownership audit in progress"),
        ("failed", None, "Ownership safety is unknown"),
        ("not_audited", None, "Ownership safety has not been checked"),
    ],
)
def test_ownership_page_audit_states(client, status, count, expected):
    audit = {
        "status": status,
        "conflict_count": count,
        "error": "Test failure" if status == "failed" else None,
    }

    with (
        patch.object(pages, "latest_scan", return_value={"id": "test-scan"}),
        patch.object(pages, "ownership_audit_summary", return_value=audit),
        patch.object(pages, "ownership_audit_conflicts") as retrieve,
    ):
        response = client.get("/review/ownership-conflicts")

    assert response.status_code == 200
    assert expected in response.text
    assert f'data-ownership-audit-status="{status}"' in response.text
    assert "data-ownership-conflict>" not in response.text

    if status != "completed":
        retrieve.assert_not_called()


def test_ownership_page_escapes_conflict_content(client, conflict):
    with (
        patch.object(pages, "latest_scan", return_value={"id": "test-scan"}),
        patch.object(
            pages,
            "ownership_audit_summary",
            return_value={"status": "completed", "conflict_count": 1},
        ),
        patch.object(
            pages,
            "ownership_audit_conflicts",
            return_value=[conflict],
        ),
    ):
        response = client.get("/review/ownership-conflicts")

    assert response.status_code == 200
    assert "Manual review is required" in response.text
    assert "&lt;script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;unsafe&gt;" in response.text
    assert response.text.count("data-ownership-conflict>") == 1


def test_ownership_page_without_scan(client):
    with (
        patch.object(pages, "latest_scan", return_value=None),
        patch.object(pages, "ownership_audit_summary") as summary,
        patch.object(pages, "ownership_audit_conflicts") as retrieve,
    ):
        response = client.get("/review/ownership-conflicts")

    assert response.status_code == 200
    assert 'data-ownership-audit-status="not_audited"' in response.text
    summary.assert_not_called()
    retrieve.assert_not_called()


@pytest.mark.parametrize(
    "failing_function",
    [
        "latest_scan",
        "ownership_audit_summary",
        "ownership_audit_conflicts",
    ],
)
def test_ownership_page_database_failure_is_safe(client, failing_function):
    """Database failures must never present incomplete findings as clean."""
    with (
        patch.object(
            pages,
            "latest_scan",
            return_value={"id": "test-scan"},
        ) as latest,
        patch.object(
            pages,
            "ownership_audit_summary",
            return_value={"status": "completed", "conflict_count": 1},
        ) as summary,
        patch.object(
            pages,
            "ownership_audit_conflicts",
            return_value=[{"parent_file_id": 1}],
        ) as conflicts,
    ):
        targets = {
            "latest_scan": latest,
            "ownership_audit_summary": summary,
            "ownership_audit_conflicts": conflicts,
        }
        targets[failing_function].side_effect = RuntimeError(
            "Simulated database failure"
        )

        response = client.get("/review/ownership-conflicts")

    assert response.status_code == 200
    assert 'data-ownership-audit-status="unavailable"' in response.text
    assert "Ownership audit status unavailable" in response.text
    assert "No ownership conflicts detected" not in response.text
    assert "data-ownership-conflict>" not in response.text
    assert "Simulated database failure" not in response.text
