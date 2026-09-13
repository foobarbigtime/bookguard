from pathlib import Path

import pytest

import app.automatic as automatic
from app.automatic import AutomaticMaintenanceError, _source_grab_event
from app.bindery_client import evaluate_replacement_candidate
from app.config import settings


def test_source_grab_requires_exact_import_grab_title_pair():
    items = [
        {"id": 20, "eventType": "bookImported", "sourceTitle": "Cat Hickey - Death of a Texan (epub)"},
        {"id": 19, "eventType": "grabbed", "sourceTitle": "Cat Hickey - Death of a Texan (epub)"},
        {"id": 18, "eventType": "grabbed", "sourceTitle": "Unrelated release"},
    ]
    event = _source_grab_event(items)
    assert event is not None
    assert event["id"] == 19


def test_source_grab_refuses_to_guess_without_pair():
    items = [
        {"id": 20, "eventType": "bookImported", "sourceTitle": "Imported release"},
        {"id": 19, "eventType": "grabbed", "sourceTitle": "Different grab"},
    ]
    assert _source_grab_event(items) is None


def test_automatic_candidate_requires_expected_author():
    result = {
        "approved": True,
        "title": "Cat of Death! retail epub",
    }
    decision = evaluate_replacement_candidate(
        result,
        expected_title="Cat of Death!",
        expected_author="",
    )
    assert decision.safe is False
    assert "author" in decision.reason.lower()


def test_same_wrong_release_on_different_indexer_stays_blocked_by_bookguard_gate():
    first = {
        "approved": False,
        "rejection": "release is blocklisted",
        "guid": "althub-guid",
        "title": "Cat Hickey - Death of a Texan (epub)",
    }
    second = {
        "approved": True,
        "guid": "nzbfinder-different-guid",
        "title": "Cat Hickey - Death of a Texan (epub)",
    }

    first_decision = evaluate_replacement_candidate(
        first,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )
    second_decision = evaluate_replacement_candidate(
        second,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )

    assert first_decision.safe is False
    assert second_decision.safe is False


def test_wrong_content_move_uses_separate_writable_alias(tmp_path, monkeypatch):
    source = tmp_path / "books" / "Book.epub"
    alias = tmp_path / "action-books" / "Book.epub"
    destination = tmp_path / "quarantine" / "Book.epub"
    for path in (source, alias):
        path.parent.mkdir()
        path.write_bytes(b"same verified bytes")

    result = {
        "id": 1,
        "scan_id": "scan-1",
        "book_id": 2,
        "file_id": 3,
        "format": "ebook",
        "title": "Book",
        "author": "Author",
        "local_path": str(source),
        "stored_path": "/data/media/books/Book.epub",
    }
    monkeypatch.setattr(settings, "allow_actions", True)
    monkeypatch.setattr(automatic, "_current_result_guard", lambda item: None)
    monkeypatch.setattr(
        automatic,
        "verify_result",
        lambda item, force: {"verdict": "WRONG_CONTENT", "confidence": 99},
    )
    monkeypatch.setattr(
        automatic,
        "wrong_content_preview",
        lambda item, client: {"safe": True, "localPath": str(source)},
    )
    monkeypatch.setattr(
        automatic,
        "resolve_writable_ebook_path",
        lambda local_path, stored_path: alias,
    )
    monkeypatch.setattr(
        automatic,
        "_quarantine_destination",
        lambda item, path: destination,
    )

    moved = []

    def reject_move(src, dst):
        moved.append((Path(src), Path(dst)))
        raise OSError("intentional unit-test stop")

    monkeypatch.setattr(automatic.shutil, "move", reject_move)

    with pytest.raises(
        AutomaticMaintenanceError,
        match="failed before Bindery was changed",
    ):
        automatic.remediate_wrong_content(result, client=object())

    assert moved == [(alias, destination)]
    assert source.is_file()
