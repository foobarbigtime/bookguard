"""Move a misfiled ebook to the Bindery book it really is.

Modelled on the Unraid case "Kill" -> "Kill Alex Cross": the file under the
truncated entry is Kill Alex Cross's missing ebook. A simulated Bindery really
moves the file and updates book_files (or deliberately does not), so the
before/after checks run against real state.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3

import pytest

from app import catalogue_move
from app.bindery_client import BinderyClientError
from app.catalogue_move import (
    CatalogueMoveError,
    move_preview,
    move_to_correct_book,
    reconcile_move,
)
from app.config import settings

PREFIX = "/data/media/books"
SOURCE = f"{PREFIX}/James Patterson/Kill ()/Kill - James Patterson.azw3"
DESTINATION = f"{PREFIX}/James Patterson/Kill Alex Cross (2011)/Kill Alex Cross - James Patterson.azw3"
DATA = b"Kill Alex Cross, the verified bytes"


@pytest.fixture
def world(tmp_path, monkeypatch):
    library = tmp_path / "books"
    source = library / "James Patterson" / "Kill ()" / "Kill - James Patterson.azw3"
    source.parent.mkdir(parents=True)
    source.write_bytes(DATA)
    bindery = tmp_path / "bindery.db"
    with sqlite3.connect(bindery) as conn:
        conn.executescript(
            """
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT,
                                     path TEXT, size_bytes INTEGER NOT NULL DEFAULT 0);
            """
        )
        conn.execute("INSERT INTO book_files(book_id, format, path) VALUES (20, 'ebook', ?)", (SOURCE,))
        conn.execute("INSERT INTO book_files(book_id, format, path) VALUES (21, 'audiobook', '/a/Kill Alex Cross')")
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(settings, "config_dir", str(config))
    monkeypatch.setattr(settings, "bindery_db", str(bindery))
    monkeypatch.setattr(settings, "ebook_root", str(library))
    monkeypatch.setattr(settings, "ebook_bindery_prefix", PREFIX)
    monkeypatch.setattr(settings, "allow_actions", True)
    return {"library": library, "bindery": bindery}


def local(world, bindery_path: str):
    return world["library"] / bindery_path[len(PREFIX) + 1:]


class FakeBindery:
    """Answers like Bindery; ``behaviour`` decides what the reassign actually does."""

    def __init__(self, world, *, mode="auto", preview_status="move", behaviour="move"):
        self.world, self.mode, self.preview_status, self.behaviour = world, mode, preview_status, behaviour
        self.reassign_calls = 0

    def get_setting(self, key):
        return self.mode

    def preview_manual_reassignment(self, path, target_book_id, *, file_format="ebook"):
        return {"source": path, "destination": DESTINATION, "format": file_format, "status": self.preview_status}

    def reassign_manual_import(self, path, target_book_id, *, file_format="ebook"):
        self.reassign_calls += 1
        if self.behaviour == "refuse":
            raise BinderyClientError("409 conflict")
        if self.behaviour == "later":
            return {"status": "accepted"}
        complete(self.world, tampered=self.behaviour == "tamper")
        return {"status": "accepted"}


def complete(world, *, tampered=False):
    """What Bindery does in the background: move, re-register, clean up."""
    destination = local(world, DESTINATION)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(local(world, SOURCE)), destination)
    if tampered:
        destination.write_bytes(b"something else")
    with sqlite3.connect(world["bindery"]) as conn:
        conn.execute("DELETE FROM book_files WHERE path = ?", (SOURCE,))
        conn.execute("INSERT INTO book_files(book_id, format, path) VALUES (21, 'ebook', ?)", (DESTINATION,))


RESULT = {"id": 5, "book_id": 20, "format": "ebook", "stored_path": SOURCE}


def proven(verdict="WRONG_CONTENT", relationship="missing_ebook"):
    return {
        "verdict": verdict,
        "evidence": {
            "catalogue": {"relationship": relationship, "bookId": 21, "title": "Kill Alex Cross"},
            "security": {"sourceSnapshot": {"sha256": hashlib.sha256(DATA).hexdigest()}},
        },
    }


def verify_returning(verification):
    def verify(result, force=False):
        assert force is True  # the move always re-proves at its own boundary
        return verification
    return verify


# --- preview ----------------------------------------------------------------


def test_proven_missing_ebook_is_eligible_with_the_named_destination(world):
    preview = move_preview(RESULT, proven(), FakeBindery(world))

    assert preview["eligible"] is True
    assert preview["blockers"] == []
    assert preview["destination"] == DESTINATION
    assert preview["targetTitle"] == "Kill Alex Cross"


@pytest.mark.parametrize(
    ("change", "blocker"),
    [
        ({"actions": False}, "binderyActionsDisabled"),
        ({"mode": "external"}, "binderyImportModeNotNormal"),
        ({"preview_status": "collision"}, "binderyPreview:collision"),
        ({"target_has_ebook": True}, "targetAlreadyHasEbook"),
        ({"other_owner": True}, "sourceNotTrackedByThisBookOnly"),
        ({"relationship": "duplicate_edition"}, "notProvenMissingEbookOfAnotherBook"),
    ],
)
def test_each_unsafe_condition_blocks_the_move(world, monkeypatch, change, blocker):
    if change.get("actions") is False:
        monkeypatch.setattr(settings, "allow_actions", False)
    with sqlite3.connect(world["bindery"]) as conn:
        if change.get("target_has_ebook"):
            conn.execute("INSERT INTO book_files(book_id, format, path) VALUES (21, 'ebook', '/x.epub')")
        if change.get("other_owner"):
            conn.execute("INSERT INTO book_files(book_id, format, path) VALUES (99, 'ebook', ?)", (SOURCE,))
    client = FakeBindery(world, mode=change.get("mode", "auto"), preview_status=change.get("preview_status", "move"))

    preview = move_preview(RESULT, proven(relationship=change.get("relationship", "missing_ebook")), client)

    assert preview["eligible"] is False
    assert blocker in preview["blockers"]


# --- the move ---------------------------------------------------------------


def test_move_is_confirmed_by_registration_and_fingerprint(world):
    client = FakeBindery(world)

    move = move_to_correct_book(RESULT, "MOVE_TO_CORRECT_BOOK", verify=verify_returning(proven()),
                                client=client, sleep=lambda s: None)

    assert move["status"] == "confirmed"
    assert move["detail"]["observed"]["fingerprintMatches"] is True
    assert move["detail"]["observed"]["oldCopyStillOnDisk"] is False
    assert client.reassign_calls == 1


def test_unfinished_move_stays_pending_and_reconciles_without_asking_again(world):
    client = FakeBindery(world, behaviour="later")

    move = move_to_correct_book(RESULT, "MOVE_TO_CORRECT_BOOK", verify=verify_returning(proven()),
                                client=client, sleep=lambda s: None, timeout_seconds=0)
    assert move["status"] == "pending"

    complete(world)
    again = reconcile_move(move["id"])

    assert again["status"] == "confirmed"
    assert client.reassign_calls == 1


def test_a_different_file_at_the_destination_is_flagged(world):
    move = move_to_correct_book(RESULT, "MOVE_TO_CORRECT_BOOK", verify=verify_returning(proven()),
                                client=FakeBindery(world, behaviour="tamper"), sleep=lambda s: None)

    assert move["status"] == "fingerprint_mismatch"


def test_bindery_refusal_is_recorded_and_reported(world):
    with pytest.raises(CatalogueMoveError, match="Bindery refused"):
        move_to_correct_book(RESULT, "MOVE_TO_CORRECT_BOOK", verify=verify_returning(proven()),
                             client=FakeBindery(world, behaviour="refuse"), sleep=lambda s: None)

    assert catalogue_move.list_moves()[0]["status"] == "request_failed"
    assert local(world, SOURCE).exists()


def test_without_the_confirmation_nothing_is_asked(world):
    client = FakeBindery(world)

    with pytest.raises(CatalogueMoveError, match="confirmation"):
        move_to_correct_book(RESULT, "yes", verify=verify_returning(proven()), client=client)

    assert client.reassign_calls == 0


def test_a_proof_that_no_longer_holds_stops_the_move(world):
    client = FakeBindery(world)

    with pytest.raises(CatalogueMoveError, match="notProvenMissingEbookOfAnotherBook"):
        move_to_correct_book(RESULT, "MOVE_TO_CORRECT_BOOK",
                             verify=verify_returning(proven(verdict="INSUFFICIENT_EVIDENCE")), client=client)

    assert client.reassign_calls == 0
    assert catalogue_move.list_moves() == []


def test_preview_shows_the_file_language_without_blocking_the_move(world):
    swedish = {**RESULT, "metadata": {"language_detection": {"languages": ["sv"]}}}
    preview = move_preview(swedish, proven(), FakeBindery(world))
    assert preview["eligible"] is True
    assert preview["language"]["label"] == "Swedish"
    assert preview["language"]["nonEnglish"] is True
    assert move_preview(RESULT, proven(), FakeBindery(world))["language"]["declared"] is False
