from pathlib import Path

import pytest

import app.automatic as automatic
import app.quarantine_fs as quarantine_fs
from app.automatic import AutomaticMaintenanceError, _source_grab_event
from app.bindery_client import evaluate_replacement_candidate
from app.config import settings
from app.scan_guard import CurrentScanError, STALE_RESULT


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


def test_shared_scan_guard_error_is_translated_for_automatic_maintenance(monkeypatch):
    def reject_result(result):
        raise CurrentScanError(
            STALE_RESULT,
            "The result is not from the latest completed scan.",
        )

    monkeypatch.setattr(automatic, "require_current_scan_result", reject_result)

    with pytest.raises(
        AutomaticMaintenanceError,
        match="not from the latest completed scan",
    ):
        automatic._require_current_scan_evidence({"scan_id": "old-scan"})


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
    monkeypatch.setattr(automatic, "require_current_scan_result", lambda item: None)
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

    monkeypatch.setattr(quarantine_fs, "_move_no_replace", reject_move)

    with pytest.raises(
        AutomaticMaintenanceError,
        match="failed before Bindery was changed",
    ):
        automatic.remediate_wrong_content(result, client=object())

    assert moved == [(alias, destination)]
    assert source.is_file()


def test_automatic_quarantine_rejects_root_that_contains_media(tmp_path, monkeypatch):
    quarantine_root = tmp_path / "media"
    ebook_root = quarantine_root / "books"
    audiobook_root = tmp_path / "audiobooks"
    source = ebook_root / "Book.epub"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"book")
    audiobook_root.mkdir()

    monkeypatch.setattr(automatic.settings, "quarantine_root", str(quarantine_root))
    monkeypatch.setattr(automatic.settings, "ebook_root", str(ebook_root))
    monkeypatch.setattr(automatic.settings, "audiobook_root", str(audiobook_root))

    with pytest.raises(
        AutomaticMaintenanceError,
        match="outside configured media roots",
    ):
        automatic._quarantine_destination({"book_id": 1}, source)



def test_unsafe_media_preview_rejects_non_deterministic_unsafe_source(tmp_path, monkeypatch):
    source = tmp_path / "books" / "Unsafe.epub"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"unsafe fixture")

    result = {
        "id": 10,
        "scan_id": "scan-current",
        "book_id": 20,
        "file_id": 30,
        "format": "ebook",
        "title": "Unsafe",
        "author": "Fixture",
        "local_path": str(source),
        "stored_path": "/data/media/books/Unsafe.epub",
    }

    class Client:
        def get_book(self, book_id):
            return {
                "id": book_id,
                "bookFiles": [{"path": result["stored_path"]}],
            }

    monkeypatch.setattr(automatic, "require_current_scan_result", lambda item: None)
    monkeypatch.setattr(
        automatic,
        "verification_for_result",
        lambda item: {
            "id": 1,
            "signature": "verification",
            "verdict": "UNSAFE_FILE",
            "confidence": 100,
            "source": "source-snapshot",
            "evidence": {
                "security": {
                    "safe": False,
                    "sourceSnapshot": {
                        "sha256": automatic.sha256_file(source),
                        "sourceStable": True,
                    },
                }
            },
        },
    )
    monkeypatch.setattr(
        automatic,
        "bindery_file_by_id",
        lambda file_id: {
            "file_id": file_id,
            "book_id": result["book_id"],
            "format": "ebook",
            "stored_path": result["stored_path"],
        },
    )
    monkeypatch.setattr(
        automatic,
        "associations_inside_path",
        lambda stored_path: [{"file_id": result["file_id"]}],
    )
    monkeypatch.setattr(
        automatic,
        "ebook_action_preview",
        lambda local_path, stored_path: {
            "ready": True,
            "checks": {},
            "blockers": [],
            "writablePath": local_path,
        },
    )

    preview = automatic.unsafe_media_preview(result, Client())

    assert preview["safe"] is False
    assert preview["checks"]["deterministicUnsafeVerdict"] is False
    assert "deterministicUnsafeVerdict" in preview["blockers"]


def test_quarantine_unsafe_media_moves_verified_bytes_and_never_deletes(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "books" / "Unsafe.epub"
    destination = tmp_path / "quarantine" / "20" / "Unsafe.epub"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b"deterministically unsafe fixture")
    expected_sha = automatic.sha256_file(source)

    result = {
        "id": 10,
        "scan_id": "scan-current",
        "book_id": 20,
        "file_id": 30,
        "format": "ebook",
        "title": "Unsafe",
        "author": "Fixture",
        "local_path": str(source),
        "stored_path": "/data/media/books/Unsafe.epub",
    }

    class Client:
        def __init__(self):
            self.detached = []

        def deregister_file(self, book_id, stored_path):
            self.detached.append((book_id, stored_path))
            return {"ok": True}

    client = Client()
    moved = []

    monkeypatch.setattr(settings, "allow_actions", True)
    monkeypatch.setattr(automatic, "require_current_scan_result", lambda item: None)
    monkeypatch.setattr(
        automatic,
        "verify_result",
        lambda item, force: {
            "verdict": "UNSAFE_FILE",
            "confidence": 100,
            "source": "deterministic-safety",
        },
    )
    monkeypatch.setattr(
        automatic,
        "unsafe_media_preview",
        lambda item, current_client: {
            "safe": True,
            "localPath": str(source),
            "expectedSha256": expected_sha,
        },
    )
    monkeypatch.setattr(
        automatic,
        "resolve_writable_ebook_path",
        lambda local_path, stored_path: source,
    )
    monkeypatch.setattr(
        automatic,
        "_quarantine_destination",
        lambda item, current_source: destination,
    )
    monkeypatch.setattr(
        automatic,
        "move_to_quarantine",
        lambda mutation_source, observed_source, target, expected_sha256=None: moved.append(
            (mutation_source, observed_source, target, expected_sha256)
        ),
    )
    monkeypatch.setattr(
        automatic,
        "bindery_file_by_id",
        lambda file_id: None,
    )

    def commit_without_failure(commit, *args, **kwargs):
        commit()

    monkeypatch.setattr(
        automatic,
        "commit_quarantine_or_rollback",
        commit_without_failure,
    )

    outcome = automatic.quarantine_unsafe_media(result, client)

    assert moved == [(source, source, destination, expected_sha)]
    assert client.detached == [(20, "/data/media/books/Unsafe.epub")]
    assert outcome["sha256"] == expected_sha
    assert outcome["binderyDetached"] is True
    assert outcome["permanentDeletion"] is False
    assert outcome["replacementRequested"] is False
