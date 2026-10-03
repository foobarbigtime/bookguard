"""Home summary and Review grouping, against a seeded scan and verifications."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import home
from app.config import settings
from app.db import add_result, create_scan, finish_scan, init_local_db, local_conn
from app.library_review import group_counts, open_review_items
from app.routes.pages import router as pages_router
from app.routes.system import router as system_router
from app.triage import init_triage_db, save_keep_decision
from app.db import result_by_id
from app.verifier import init_verification_db

SCAN = "home-scan"

# title, classification, verdict, evidence
BOOKS = [
    ("Mary, Mary", "REJECT", "WRONG_CONTENT", {"embedded": {"title": "Mary and Mr Eliot", "author": "Someone Else"}}),
    ("Kill", "REJECT", "WRONG_CONTENT", {"catalogue": {"relationship": "missing_ebook", "bookId": 21, "title": "Kill Alex Cross"}}),
    ("Cross", "REVIEW", "WRONG_CONTENT", {"catalogue": {"relationship": "duplicate_identical", "bookId": 31, "title": "Cross Fire"}}),
    ("Young Blood", "REJECT", "UNSAFE_FILE", {"explanation": "The archive is corrupt."}),
    ("Sooley", "REVIEW", "METADATA_ERROR", {}),
    ("Black Friday", "REVIEW", "VERIFIED_CORRECT", {}),
    ("Chase", "REVIEW", "INSUFFICIENT_EVIDENCE", {}),
    ("Unverified", "REVIEW", None, {}),
    ("Outage", "REVIEW", "INSUFFICIENT_EVIDENCE", {"malwareScanInconclusive": True}),
    ("Reviewed already", "REVIEW", "WRONG_CONTENT", {}),
    ("Fine", "PASS", None, {}),
]


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "bindery_db", str(tmp_path / "bindery.db"))
    monkeypatch.setattr(settings, "verification_malware_scan", False)
    monkeypatch.setattr(settings, "verification_clamd_host", "")
    (tmp_path / "bindery.db").write_bytes(b"")
    init_local_db()
    init_triage_db()
    init_verification_db()
    create_scan(SCAN, len(BOOKS))
    for index, (title, classification, _, _) in enumerate(BOOKS, start=1):
        add_result(SCAN, {
            "file_id": index, "book_id": 100 + index, "author": "James Patterson", "title": title,
            "format": "ebook", "stored_path": f"/data/media/books/{title}.epub",
            "local_path": f"/books/{title}.epub", "classification": classification,
            "risk_score": 50 + index, "reason_code": "MISMATCH", "reasons": [f"{title} flagged"],
            "metadata": {"language_detection": {"languages": ["swe"] if title == "Kill" else ["en"]}},
        })
    finish_scan(SCAN)
    ids = {}
    with local_conn() as conn:
        for row in conn.execute("SELECT id, title FROM scan_results WHERE scan_id=?", (SCAN,)):
            ids[row["title"]] = int(row["id"])
        for title, _, verdict, evidence in BOOKS:
            if verdict is None:
                continue
            conn.execute(
                """INSERT INTO content_verifications(signature, result_id, scan_id, file_id, book_id, format,
                       author, title, target_path, file_fingerprint, verdict, confidence, source,
                       evidence_json, created_at, updated_at)
                   VALUES (?, ?, ?, 1, 1, 'ebook', 'James Patterson', ?, '/x', 'f', ?, 90, 'native',
                       ?, '2026-10-01T10:00:00+00:00', '2026-10-01T10:00:00+00:00')""",
                (f"sig-{title}", ids[title], SCAN, title, verdict, json.dumps(evidence)),
            )
        conn.commit()
    monkeypatch.setattr("app.triage._require_current_triage_result", lambda result: None)
    save_keep_decision(result_by_id(ids["Reviewed already"]))
    monkeypatch.setattr(home, "attention_snapshot", lambda: {"items": []})
    return ids


def test_open_items_are_grouped_by_the_decision_they_need(library):
    items = {item["title"]: item for item in open_review_items()}
    assert {title: item["group"] for title, item in items.items()} == {
        "Mary, Mary": "wrong",
        "Kill": "move",
        "Cross": "duplicate",
        "Young Blood": "unsafe",
        "Sooley": "metadata",
        "Black Friday": "verified",
        "Chase": "undecided",
        "Unverified": "undecided",
        "Outage": "undecided",
    }
    assert "Reviewed already" not in items and "Fine" not in items
    assert items["Mary, Mary"]["contains"] == "Mary and Mr Eliot by Someone Else"
    assert items["Kill"]["otherBook"] == {"id": 21, "title": "Kill Alex Cross"}
    assert "Kill Alex Cross" in items["Kill"]["suggestion"]
    assert items["Unverified"]["verified"] is False
    # A scanner outage proves nothing: the item is undecided and counted as unverified.
    assert items["Outage"]["verified"] is False
    assert list(items)[0] == "Young Blood"


def test_home_summary_groups_decisions_and_counts_the_library(library):
    summary = home.home_summary()
    assert summary["health"]["level"] == "ok"
    titles = [group["title"] for group in summary["attention"]]
    assert titles == [
        "1 damaged or unsafe file",
        "1 ebook is filed under the wrong book",
        "1 file is a copy of another book",
        "1 book contains the wrong file",
        "1 book has the right file but wrong details",
        "3 books are undecided",
    ]
    assert summary["decisions"] == 5
    assert summary["library"] == {
        **summary["library"],
        "checked": 9, "passed": 1, "verified": 1, "wrongFile": 3, "damaged": 1, "details": 1, "undecided": 3,
    }
    undecided = summary["attention"][-1]
    assert undecided["why"] == "2 have not been verified yet."
    assert summary["automation"]["mode"] == "manual"


def test_home_health_reports_a_missing_bindery_database(library, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "bindery_db", str(tmp_path / "absent.db"))
    health = home.home_summary()["health"]
    assert health["level"] == "error"
    assert "Bindery's database" in health["problems"][0]["text"]


def test_home_and_review_pages_render(library):
    app = FastAPI()
    app.include_router(pages_router)
    app.include_router(system_router)
    with TestClient(app) as client:
        page = client.get("/").text
        assert "BookGuard is healthy" in page
        assert "1 ebook is filed under the wrong book" in page
        assert 'href="/review?group=move"' in page
        assert client.get("/api/home").json()["decisions"] == 5

        review = client.get("/review").text
        assert "Kill Alex Cross" in review
        assert "Damaged or unsafe files <b>1</b>" in review
        moves = client.get("/review?group=move").text
        assert 'data-review-id="%d"' % library["Kill"] in moves
        assert 'data-review-id="%d"' % library["Mary, Mary"] not in moves
        several = client.get("/review?group=move,duplicate,wrong").text
        assert 'data-review-id="%d"' % library["Mary, Mary"] in several
        assert 'data-review-id="%d"' % library["Young Blood"] not in several
        assert 'data-review-id="%d"' % library["Young Blood"] in client.get("/review?group=bogus").text
        assert client.get("/review/triage").status_code == 200
        assert client.get("/review/scan-results").status_code == 200


def test_group_counts_cover_every_group(library):
    counts = group_counts(open_review_items())
    assert sum(counts.values()) == 9


def test_home_still_loads_when_the_attention_queue_cannot_be_read(library, monkeypatch):
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(home, "attention_snapshot", broken)
    summary = home.home_summary()
    assert summary["health"]["level"] == "error"
    assert "the attention queue: database is locked" in summary["health"]["problems"][-1]["text"]
    assert summary["attention"][0]["key"] == "unsafe"


def test_review_shows_the_declared_language_and_filters_books_not_in_english(library):
    items = {item["title"]: item for item in open_review_items()}
    assert items["Kill"]["language"] == {"codes": ["sv"], "label": "Swedish", "declared": True, "nonEnglish": True}
    assert items["Mary, Mary"]["language"]["nonEnglish"] is False

    app = FastAPI()
    app.include_router(pages_router)
    with TestClient(app) as client:
        page = client.get("/review").text
        assert '<span class="lang-tag">Swedish</span>' in page
        assert "Not in English <b>1</b>" in page
        filtered = client.get("/review?language=other").text
        assert 'data-review-id="%d"' % library["Kill"] in filtered
        assert 'data-review-id="%d"' % library["Mary, Mary"] not in filtered
