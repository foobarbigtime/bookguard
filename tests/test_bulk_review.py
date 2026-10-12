"""Bulk fixes: the preview is read-only and reports each book's own blockers."""

from __future__ import annotations

import pytest

from app import bulk_review
from app.config import settings


def item(result_id, group="move", mixed=False):
    return {"id": result_id, "title": f"Book {result_id}", "author": "A", "group": group, "mixedAudio": mixed}


@pytest.fixture
def plan(monkeypatch):
    monkeypatch.setattr(settings, "allow_actions", True)
    monkeypatch.setattr(settings, "metadata_repair_mode", "safe")
    monkeypatch.setattr(bulk_review, "result_by_id", lambda rid: {"id": rid, "book_id": rid, "format": "ebook", "stored_path": f"/p/{rid}"})
    monkeypatch.setattr(bulk_review, "latest_verifications", lambda ids: {rid: {"verdict": "X"} for rid in ids if rid != 3})
    return monkeypatch


def test_unsupported_group_is_rejected():
    with pytest.raises(ValueError):
        bulk_review.bulk_preview("duplicate")


def test_move_preview_lists_eligible_and_blocked_books(plan):
    plan.setattr(bulk_review, "open_review_items", lambda: [item(1), item(2), item(3), item(4, group="wrong")])

    def fake_move(row, verification, client):
        ok = row["id"] == 1
        return {"eligible": ok, "blockers": [] if ok else ["targetAlreadyHasEbook"], "targetTitle": "Target"}

    plan.setattr(bulk_review, "move_preview", fake_move)
    result = bulk_review.bulk_preview("move", client=object())
    assert (result["eligible"], result["total"]) == (1, 3)
    by_id = {book["id"]: book for book in result["books"]}
    assert by_id[1]["eligible"] and "Target" in by_id[1]["detail"]
    assert by_id[2]["blockers"] == "targetAlreadyHasEbook"
    assert by_id[3]["blockers"] == "notVerified"  # no conclusive verification
    assert result["confirm"] == "MOVE_TO_CORRECT_BOOK"


def test_one_failing_preview_does_not_hide_the_rest(plan):
    plan.setattr(bulk_review, "open_review_items", lambda: [item(1), item(2)])

    def fake_move(row, verification, client):
        if row["id"] == 1:
            raise RuntimeError("bindery down")
        return {"eligible": True, "blockers": [], "targetTitle": "T"}

    plan.setattr(bulk_review, "move_preview", fake_move)
    result = bulk_review.bulk_preview("move", client=object())
    assert [book["eligible"] for book in result["books"]] == [False, True]
    assert result["books"][0]["blockers"].startswith("previewFailed")


def test_metadata_requires_safe_mode(plan):
    plan.setattr(bulk_review, "open_review_items", lambda: [item(1, "metadata")])
    plan.setattr(settings, "metadata_repair_mode", "preview")
    result = bulk_review.bulk_preview("metadata")
    assert result["eligible"] == 0
    assert result["books"][0]["blockers"] == "metadataRepairNotSafeMode"


def test_metadata_shows_before_and_after(plan):
    plan.setattr(bulk_review, "open_review_items", lambda: [item(1, "metadata")])
    plan.setattr(bulk_review, "verified_repair_preview", lambda row, v: {
        "eligible": True, "safe": True,
        "before": {"title": "Wrong", "author": "X"}, "after": {"title": "Right", "author": "Y"}})
    result = bulk_review.bulk_preview("metadata")
    assert result["books"][0]["detail"] == "Wrong / X → Right / Y"


def test_wrong_skips_books_with_another_copy_and_mixed_audio(plan):
    plan.setattr(bulk_review, "open_review_items",
                 lambda: [item(1, "wrong"), item(2, "wrong"), item(5, "wrong", mixed=True)])
    plan.setattr(bulk_review, "bindery_files_for_book",
                 lambda book, fmt: [{"stored_path": "/other"}] if book == 2 else [{"stored_path": f"/p/{book}"}])
    plan.setattr(bulk_review, "triage_action_preview", lambda row, action: {"safe": True})
    result = bulk_review.bulk_preview("wrong")
    assert [b["id"] for b in result["books"]] == [1, 2]  # mixed-audio folder excluded
    assert [b["eligible"] for b in result["books"]] == [True, False]
    assert result["books"][1]["blockers"] == "binderyHasAnotherCopy"


def test_actions_disabled_blocks_replace(plan):
    plan.setattr(settings, "allow_actions", False)
    plan.setattr(bulk_review, "open_review_items", lambda: [item(1, "wrong")])
    assert bulk_review.bulk_preview("wrong")["books"][0]["blockers"] == "binderyActionsDisabled"
