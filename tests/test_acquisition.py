import hashlib
from pathlib import Path
import zipfile

import pytest

from app import acquisition
from app.db import (
    ebook_acquisition_by_id,
    init_local_db,
    recent_ebook_acquisitions,
)


class FakeClient:
    def __init__(self):
        self.queue = {"items": [], "partial": False}
        self.history = {"items": []}
        self.auto_grab = "false"
        self.grabs = []
        self.removed_queue_items = []
        self.registered = False
        self.candidate = {
            "guid": "safe-guid",
            "title": "Ann Patchett - Bel Canto retail epub",
            "nzbUrl": "https://indexer.invalid/safe.nzb",
            "size": 2048,
            "approved": True,
            "mediaType": "ebook",
            "protocol": "usenet",
            "indexerId": 4,
            "indexerName": "Test Indexer",
        }

    def get_setting(self, key):
        return {
            "import.mode": "external",
            "import.drop_folder": "/data/bookguard-staging",
            "import.drop_layout": "flat",
            "import.drop_link_mode": "copy",
            "autoGrab.enabled": self.auto_grab,
        }[key]

    def list_queue(self):
        return self.queue

    def get_book(self, book_id):
        path = (
            "/data/media/books/Ann Patchett/Bel Canto (2001)/"
            "Bel Canto - Ann Patchett.epub"
        )
        return {
            "id": book_id,
            "title": "Bel Canto",
            "author": {"name": "Ann Patchett"},
            "ebookFilePath": path if self.registered else "",
            "bookFiles": (
                [{"format": "ebook", "path": path}]
                if self.registered
                else []
            ),
        }

    def search_book(self, book_id):
        return {"results": [self.candidate]}

    def list_history(self, book_id, event_type=None, limit=100):
        return self.history

    def grab(self, book_id, candidate):
        self.grabs.append((book_id, candidate["guid"]))
        return {"queueItem": {"id": 77}}

    def remove_queue_item(
        self,
        queue_id,
        *,
        remove_from_client=False,
        delete_files=False,
    ):
        self.removed_queue_items.append(
            (queue_id, remove_from_client, delete_files)
        )
        self.queue["items"] = [
            item
            for item in self.queue["items"]
            if int(item.get("id") or 0) != int(queue_id)
        ]


def _write_epub(path: Path, title: str, author: str) -> None:
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    package = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:identifier>acquisition-test</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    body = f"{title} by {author}. " + ("A fictional passage. " * 100)
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f"<h1>{title}</h1><p>by {author}</p><p>{body}</p>"
        "</body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


@pytest.fixture
def acquisition_setup(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    books = tmp_path / "books"
    quarantine = tmp_path / "quarantine"
    admission = tmp_path / "admission"
    config = tmp_path / "config"
    relative = Path("Ann Patchett/Bel Canto (2001)/Bel Canto - Ann Patchett.epub")
    for path in (staging, books, quarantine, admission, config):
        path.mkdir()
    (books / relative.parent).mkdir(parents=True)
    (admission / relative.parent).mkdir(parents=True)

    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.setenv(
        "BOOKGUARD_BINDERY_DROP_FOLDER",
        "/data/bookguard-staging",
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ENABLED", "true")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ROOT", str(admission))
    monkeypatch.setenv(
        "BOOKGUARD_ADMISSION_BINDERY_ROOT",
        "/data/media/books",
    )
    monkeypatch.setattr(acquisition.settings, "allow_actions", True)
    monkeypatch.setattr(acquisition.settings, "ebook_root", str(books))
    monkeypatch.setattr(
        acquisition.settings,
        "ebook_bindery_prefix",
        "/data/media/books",
    )
    monkeypatch.setattr(
        acquisition.settings,
        "audiobook_root",
        str(tmp_path / "audiobooks"),
    )
    monkeypatch.setattr(
        acquisition.settings,
        "quarantine_root",
        str(quarantine),
    )
    monkeypatch.setattr(acquisition.settings, "config_dir", str(config))
    monkeypatch.setattr(
        acquisition,
        "latest_scan",
        lambda: {"id": "scan-1", "status": "complete"},
    )
    init_local_db()

    result = {
        "id": 17,
        "scan_id": "scan-1",
        "file_id": 22,
        "book_id": 42,
        "title": "Bel Canto",
        "author": "Ann Patchett",
        "format": "ebook",
        "classification": "REVIEW",
        "stored_path": str(Path("/data/media/books") / relative),
        "local_path": str(books / relative),
    }
    monkeypatch.setattr(
        acquisition,
        "result_by_id",
        lambda result_id: result if result_id == 17 else None,
    )
    return {
        "staging": staging,
        "result": result,
        "client": FakeClient(),
    }


def test_readiness_requires_actions_and_automatic_opt_in(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    monkeypatch.setattr(acquisition.settings, "allow_actions", False)
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "false")

    result = acquisition.acquisition_readiness(setup["client"])

    assert result["ready"] is False
    assert "actionsEnabled" in result["blockers"]
    assert "automaticReacquisitionEnabled" in result["blockers"]


def test_readiness_requires_disabled_auto_grab_and_complete_idle_queue(
    acquisition_setup,
):
    client = acquisition_setup["client"]
    client.auto_grab = "true"
    client.queue = {
        "items": [{"id": 8, "status": "downloading"}],
        "partial": True,
    }

    result = acquisition.acquisition_readiness(client)

    assert result["ready"] is False
    assert "binderyAutoGrabDisabled" in result["blockers"]
    assert "binderyQueueComplete" in result["blockers"]
    assert "binderyQueueIdle" in result["blockers"]


def test_start_revalidates_and_records_one_explicit_grab(acquisition_setup):
    setup = acquisition_setup

    result = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )

    record = result["acquisition"]
    assert record["status"] == "queued"
    assert record["queue_id"] == 77
    assert record["candidate_guid"] == "safe-guid"
    assert record["grab_response"] == {
        "id": 77,
        "bookId": None,
        "title": None,
        "status": None,
        "protocol": None,
    }
    assert setup["client"].grabs == [(42, "safe-guid")]


def test_start_rejects_release_that_fails_identity_gate(acquisition_setup):
    setup = acquisition_setup
    setup["client"].candidate["title"] = "Different Writer - Other Book epub"

    with pytest.raises(acquisition.AcquisitionSafetyError, match="safety gate"):
        acquisition.start_ebook_acquisition(
            setup["result"],
            "safe-guid",
            setup["client"],
        )

    assert setup["client"].grabs == []
    assert recent_ebook_acquisitions() == []


def test_start_rejects_exact_release_already_imported(acquisition_setup):
    setup = acquisition_setup
    setup["client"].history = {
        "items": [
            {
                "eventType": "grabbed",
                "sourceTitle": setup["client"].candidate["title"],
            },
            {
                "eventType": "bookImported",
                "sourceTitle": setup["client"].candidate["title"],
            },
        ]
    }

    with pytest.raises(acquisition.AcquisitionSafetyError, match="history"):
        acquisition.start_ebook_acquisition(
            setup["result"],
            "safe-guid",
            setup["client"],
        )

    assert setup["client"].grabs == []
    assert recent_ebook_acquisitions() == []


def test_reconcile_verifies_exactly_one_staged_ebook(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "importExternal"}],
        "partial": False,
    }

    observed = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    result = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert observed["acquisition"]["status"] == "staging_observed"
    record = result["acquisition"]
    assert record["status"] == "verified"
    assert record["queue_status"] == "importexternal"
    assert record["staged_relative_path"] == staged.name
    assert record["staged_sha256"] == hashlib.sha256(staged.read_bytes()).hexdigest()
    assert record["verification"]["safeToAdmit"] is True
    assert staged.is_file()


def test_reconcile_refuses_ambiguous_staging(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    _write_epub(setup["staging"] / "first.epub", "Bel Canto", "Ann Patchett")
    _write_epub(setup["staging"] / "second.epub", "Bel Canto", "Ann Patchett")

    result = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert result["ok"] is False
    assert result["acquisition"]["status"] == "review_required"
    assert len(result["stagedFiles"]) == 2


def test_operator_can_retry_review_required_staged_verification(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "importExternal"}],
        "partial": False,
    }

    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    monkeypatch.setattr(
        acquisition,
        "verify_staged_ebook",
        lambda *args, **kwargs: {
            "relativePath": staged.name,
            "safeToAdmit": False,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "confidence": 70,
            "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        },
    )
    reviewed = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    assert reviewed["acquisition"]["status"] == "review_required"

    monkeypatch.setattr(
        acquisition,
        "verify_staged_ebook",
        lambda *args, **kwargs: {
            "relativePath": staged.name,
            "safeToAdmit": True,
            "verdict": "VERIFIED_CORRECT",
            "confidence": 99,
            "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        },
    )
    retried = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert retried["ok"] is True
    assert retried["acquisition"]["status"] == "verified"
    assert retried["acquisition"]["staged_relative_path"] == staged.name
    assert staged.is_file()


def test_reconcile_records_failed_bindery_download(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    setup["client"].queue = {
        "items": [{
            "id": 77,
            "bookId": 42,
            "status": "failed",
            "errorMessage": "download client rejected it",
        }],
        "partial": False,
    }

    result = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert result["acquisition"]["status"] == "failed"
    assert "download client" in result["acquisition"]["error"]


def test_reconcile_does_not_verify_before_external_handoff(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "downloading"}],
        "partial": False,
    }

    response = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert response["acquisition"]["status"] == "downloading"
    assert response["acquisition"]["queue_status"] == "downloading"
    assert response["acquisition"]["staged_sha256"] is None
    assert staged.is_file()


def test_verified_acquisition_hands_off_to_guarded_admission(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    calls = []
    monkeypatch.setattr(
        acquisition,
        "admit_staged_ebook",
        lambda result, relative_path, client: calls.append(
            (result["id"], relative_path)
        ) or {"admissionId": 91, "status": "scan_requested"},
    )

    response = acquisition.admit_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert response["acquisition"]["status"] == "admitted"
    assert response["acquisition"]["admission_id"] == 91
    assert calls == [(17, staged.name)]
    assert ebook_acquisition_by_id(started["acquisition"]["id"])["admission_id"] == 91
    assert staged.is_file()


def test_changed_staged_bytes_are_blocked_before_admission(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    staged.write_bytes(staged.read_bytes() + b"changed")

    with pytest.raises(acquisition.AcquisitionSafetyError, match="changed"):
        acquisition.admit_ebook_acquisition(
            started["acquisition"]["id"],
            setup["client"],
        )

    record = ebook_acquisition_by_id(started["acquisition"]["id"])
    assert record["status"] == "verified"
    assert record["admission_id"] is None


def test_finalize_registered_acquisition_removes_only_queue_and_staging(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "importExternal"}],
        "partial": False,
    }
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    monkeypatch.setattr(
        acquisition,
        "admit_staged_ebook",
        lambda result, relative_path, client: {
            "admissionId": 91,
            "status": "scan_requested",
        },
    )
    acquisition.admit_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    record = acquisition.ebook_acquisition_by_id(
        started["acquisition"]["id"]
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "result_id": 17,
            "book_id": 42,
            "status": "registered",
            "staged_relative_path": staged.name,
            "staged_sha256": record["staged_sha256"],
            "stored_path": setup["result"]["stored_path"],
            "local_path": setup["result"]["local_path"],
        },
    )
    Path(setup["result"]["local_path"]).write_bytes(staged.read_bytes())
    setup["client"].registered = True

    response = acquisition.finalize_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert response["acquisition"]["status"] == "finalized"
    assert response["queueRecordRemoved"] is True
    assert response["removedFromDownloadClient"] is False
    assert response["downloadedDataDeleted"] is False
    assert response["stagedFileRemoved"] is True
    assert not staged.exists()
    assert setup["client"].removed_queue_items == [(77, False, False)]


def test_finalize_refuses_unregistered_admission(acquisition_setup, monkeypatch):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    acquisition.update_ebook_acquisition(
        started["acquisition"]["id"],
        "admitted",
        admission_id=91,
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {"id": admission_id, "status": "scan_requested"},
    )

    with pytest.raises(acquisition.AcquisitionSafetyError, match="not registered"):
        acquisition.finalize_ebook_acquisition(
            started["acquisition"]["id"],
            setup["client"],
        )


def test_finalize_refuses_nonterminal_queue_and_retains_staging(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    staged_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
    acquisition.update_ebook_acquisition(
        started["acquisition"]["id"],
        "admitted",
        queue_id=77,
        queue_status="downloading",
        staged_relative_path=staged.name,
        staged_sha256=staged_hash,
        admission_id=91,
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "result_id": 17,
            "book_id": 42,
            "status": "registered",
            "staged_relative_path": staged.name,
            "staged_sha256": staged_hash,
            "stored_path": setup["result"]["stored_path"],
            "local_path": setup["result"]["local_path"],
        },
    )
    Path(setup["result"]["local_path"]).write_bytes(staged.read_bytes())
    setup["client"].registered = True
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "downloading"}],
        "partial": False,
    }

    with pytest.raises(acquisition.AcquisitionSafetyError, match="not safe"):
        acquisition.finalize_ebook_acquisition(
            started["acquisition"]["id"],
            setup["client"],
        )

    assert staged.is_file()
    assert setup["client"].removed_queue_items == []


def test_interrupted_finalize_refuses_symlinked_staging_path(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    outside = setup["staging"].parent / "outside.epub"
    outside.write_bytes(b"must remain")
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    staged.symlink_to(outside)
    staged_hash = hashlib.sha256(outside.read_bytes()).hexdigest()
    acquisition.update_ebook_acquisition(
        started["acquisition"]["id"],
        "cleanup_required",
        queue_id=77,
        queue_status="removed",
        staged_relative_path=staged.name,
        staged_sha256=staged_hash,
        admission_id=91,
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "result_id": 17,
            "book_id": 42,
            "status": "registered",
            "staged_relative_path": staged.name,
            "staged_sha256": staged_hash,
            "stored_path": setup["result"]["stored_path"],
            "local_path": setup["result"]["local_path"],
        },
    )
    Path(setup["result"]["local_path"]).write_bytes(outside.read_bytes())
    setup["client"].registered = True

    with pytest.raises(acquisition.AcquisitionSafetyError, match="Symlinked"):
        acquisition.finalize_ebook_acquisition(
            started["acquisition"]["id"],
            setup["client"],
        )

    assert staged.is_symlink()
    assert outside.read_bytes() == b"must remain"


def test_finalize_reconciles_already_cleaned_registered_acquisition(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged_name = "Bel Canto - Ann Patchett.epub"
    library = Path(setup["result"]["local_path"])
    _write_epub(library, "Bel Canto", "Ann Patchett")
    staged_hash = hashlib.sha256(library.read_bytes()).hexdigest()
    acquisition.update_ebook_acquisition(
        started["acquisition"]["id"],
        "admitted",
        queue_id=77,
        queue_status="downloading",
        staged_relative_path=staged_name,
        staged_sha256=staged_hash,
        admission_id=91,
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "result_id": 17,
            "book_id": 42,
            "status": "registered",
            "staged_relative_path": staged_name,
            "staged_sha256": staged_hash,
            "stored_path": setup["result"]["stored_path"],
            "local_path": setup["result"]["local_path"],
        },
    )
    setup["client"].registered = True

    response = acquisition.finalize_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert response["acquisition"]["status"] == "finalized"
    assert response["cleanupAlreadyComplete"] is True
    assert response["removedFromDownloadClient"] is False
    assert response["downloadedDataDeleted"] is False
    assert library.is_file()
    assert setup["client"].removed_queue_items == []
