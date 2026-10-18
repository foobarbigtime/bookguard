"""Protect nested audiobook tracks from quarantine when a parent is owned elsewhere."""
import sqlite3

import pytest

import app.actions as actions
import app.triage as triage
from app.config import settings
from app.db import foreign_ancestor_associations


PARENT = "/data/media/audiobooks/Example Author/Collected Audio"
CHILD = PARENT + "/Disc 1/Track 01.mp3"


@pytest.fixture
def nested_ownership(tmp_path, monkeypatch):
    database = tmp_path / "bindery.db"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE book_files (
                id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT, path TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO books (id, title) VALUES (?, ?)",
            [(10, "Collected Audio"), (20, "Unrelated Title"), (30, "Other Book")],
        )
        conn.executemany(
            "INSERT INTO book_files (id, book_id, format, path) VALUES (?, ?, ?, ?)",
            [
                (1, 10, "audiobook", PARENT),
                (2, 20, "audiobook", CHILD),
                (3, 30, "audiobook", PARENT + " 2/Disc 1/Track 01.mp3"),
            ],
        )
    monkeypatch.setattr(settings, "bindery_db", str(database))
    physical_file = tmp_path / "Track 01.mp3"
    physical_file.write_bytes(b"synthetic audio")
    return physical_file


def test_foreign_ancestor_detects_other_book_across_nested_directories(nested_ownership):
    owners = foreign_ancestor_associations(CHILD, 20)
    assert len(owners) == 1
    assert owners[0]["file_id"] == 1
    assert owners[0]["book_id"] == 10
    assert owners[0]["title"] == "Collected Audio"


def test_foreign_ancestor_does_not_match_sibling_prefix_or_same_book(nested_ownership):
    assert foreign_ancestor_associations(PARENT + " 2/Disc 1/Track 01.mp3", 30) == []
    assert foreign_ancestor_associations(CHILD, 10) == []


def test_foreign_ancestor_rejects_nonabsolute_or_traversal_paths(nested_ownership):
    with pytest.raises(ValueError):
        foreign_ancestor_associations("Disc 1/Track 01.mp3", 20)
    with pytest.raises(ValueError):
        foreign_ancestor_associations(PARENT + "/../Track 01.mp3", 20)


def test_quarantine_blocks_nested_track_before_any_file_move(
    nested_ownership, monkeypatch
):
    monkeypatch.setattr(actions, "_require_actions", lambda: "test-api-key")
    monkeypatch.setattr(
        actions, "associations_inside_path", lambda stored_path: [{"file_id": 2}]
    )

    def must_not_move(*args, **kwargs):
        raise AssertionError("Physical move must not be attempted")

    monkeypatch.setattr(actions, "move_to_quarantine", must_not_move)

    with pytest.raises(actions.ActionError, match="book 10"):
        actions.quarantine_file(20, CHILD, str(nested_ownership))


def test_triage_preview_blocks_nested_track_even_if_source_is_writable(
    nested_ownership, monkeypatch
):
    monkeypatch.setattr(triage, "_require_current_triage_result", lambda row: None)
    monkeypatch.setattr(triage, "_exact_bindery_match", lambda row: True)
    monkeypatch.setattr(triage, "mount_is_writable", lambda path: True)
    result = {
        "file_id": 2,
        "book_id": 20,
        "format": "audiobook",
        "stored_path": CHILD,
        "local_path": str(nested_ownership),
    }

    preview = triage.triage_action_preview(result, "quarantine")
    assert preview["safe"] is False
    assert preview["eligible"] is False
    assert "book 10" in preview["reason"]
    # Detach does not remove media and remains independently available.
    assert triage.triage_action_preview(result, "detach")["safe"] is True


def test_preview_and_execution_fail_closed_if_ownership_db_unreadable(
    nested_ownership, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "bindery_db", str(tmp_path / "missing.db"))
    monkeypatch.setattr(triage, "_require_current_triage_result", lambda row: None)
    monkeypatch.setattr(triage, "_exact_bindery_match", lambda row: True)
    monkeypatch.setattr(triage, "mount_is_writable", lambda path: True)

    result = {
        "file_id": 2,
        "book_id": 20,
        "format": "audiobook",
        "stored_path": CHILD,
        "local_path": str(nested_ownership),
    }
    preview = triage.triage_action_preview(result, "quarantine")
    assert preview["safe"] is False
    assert "Cannot verify parent-directory ownership" in preview["reason"]

    monkeypatch.setattr(actions, "_require_actions", lambda: "test-api-key")
    monkeypatch.setattr(
        actions, "associations_inside_path", lambda stored_path: [{"file_id": 2}]
    )

    def must_not_move(*args, **kwargs):
        raise AssertionError("Physical move must not be attempted")

    monkeypatch.setattr(actions, "move_to_quarantine", must_not_move)
    with pytest.raises(actions.ActionError, match="cannot verify parent-directory"):
        actions.quarantine_file(20, CHILD, str(nested_ownership))
