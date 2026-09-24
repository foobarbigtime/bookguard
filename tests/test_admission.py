from __future__ import annotations

import hashlib
import errno
from pathlib import Path
import sqlite3
import zipfile

import pytest

import app.admission as admission
import app.ebook_extraction as ebook_extraction
from app.db import (
    ebook_admission_by_id,
    init_local_db,
    local_conn,
    recent_ebook_admissions,
    update_ebook_admission,
)


class FakeClient:
    def __init__(self, *, title: str = "Bel Canto", author: str = "Ann Patchett"):
        self.title = title
        self.author = author
        self.registered_path = ""
        self.scan_requests = 0
        self.scan_error: Exception | None = None
        self.setting_values = {
            "import.mode": "external",
            "import.drop_folder": "/data/bookguard-staging",
            "import.drop_layout": "flat",
            "import.drop_link_mode": "copy",
            "autoGrab.enabled": "false",
        }
        self.setting_updates = []
        self.queue_items = []
        self.removed_queue_items = []
        self.reassignment_preview = None
        self.reassignment_error: Exception | None = None
        self.on_reassign = None

    def get_setting(self, key: str):
        return self.setting_values[key]

    def set_setting(self, key: str, value):
        self.setting_values[key] = value
        self.setting_updates.append((key, value))
        return {"key": key, "value": value}

    def get_book(self, book_id: int):
        files = []
        if self.registered_path:
            files.append({"format": "ebook", "path": self.registered_path})
        return {
            "id": book_id,
            "title": self.title,
            "author": {"name": self.author},
            "ebookFilePath": self.registered_path,
            "bookFiles": files,
        }

    def scan_library(self):
        self.scan_requests += 1
        if self.scan_error:
            raise self.scan_error
        return {"message": "library scan started"}

    def list_queue(self):
        return {"items": list(self.queue_items), "partial": False}

    def remove_queue_item(
        self,
        queue_id: int,
        *,
        remove_from_client: bool = False,
        delete_files: bool = False,
    ):
        self.removed_queue_items.append(
            (queue_id, remove_from_client, delete_files)
        )
        self.queue_items = [
            item
            for item in self.queue_items
            if int(item.get("id") or 0) != queue_id
        ]

    def preview_manual_reassignment(
        self,
        tracked_path: str,
        target_book_id: int,
        *,
        file_format: str = "ebook",
    ):
        return self.reassignment_preview or {
            "source": tracked_path,
            "destination": tracked_path,
            "format": file_format,
            "status": "noop",
        }

    def reassign_manual_import(
        self,
        tracked_path: str,
        target_book_id: int,
        *,
        file_format: str = "ebook",
    ):
        if self.reassignment_error:
            raise self.reassignment_error
        if self.on_reassign:
            self.on_reassign(tracked_path, target_book_id, file_format)
        self.registered_path = tracked_path
        return {"id": 9001, "status": "completed"}


def _write_epub(path: Path, title: str, author: str, body: str) -> None:
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
    <dc:identifier>test-id</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    chapter = f"""<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>{title}</h1><p>by {author}</p><p>{body}</p>
</body></html>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


@pytest.fixture
def admission_setup(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    admission_root = tmp_path / "admission"
    read_root = tmp_path / "books-readonly"
    quarantine = tmp_path / "quarantine"
    config = tmp_path / "config"
    for path in (staging, admission_root, read_root, quarantine, config):
        path.mkdir()

    relative = Path("Ann Patchett/Bel Canto (2001)/Bel Canto - Ann Patchett.epub")
    (admission_root / relative.parent).mkdir(parents=True)
    (read_root / relative.parent).mkdir(parents=True)
    staged = staging / "Bel Canto - Ann Patchett.epub"
    _write_epub(
        staged,
        "Bel Canto",
        "Ann Patchett",
        "Bel Canto by Ann Patchett. " + ("A fictional passage. " * 80),
    )

    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/data/bookguard-staging")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ENABLED", "true")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ROOT", str(admission_root))
    monkeypatch.setenv("BOOKGUARD_ADMISSION_BINDERY_ROOT", "/data/media/books")
    monkeypatch.setattr(admission.settings, "allow_actions", True)
    monkeypatch.setattr(admission.settings, "ebook_root", str(read_root))
    monkeypatch.setattr(admission.settings, "ebook_bindery_prefix", "/data/media/books")
    monkeypatch.setattr(admission.settings, "quarantine_root", str(quarantine))
    monkeypatch.setattr(admission.settings, "config_dir", str(config))
    monkeypatch.setattr(ebook_extraction.settings, "verification_use_tika", False)
    init_local_db()

    result = {
        "id": 17,
        "scan_id": "scan-1",
        "book_id": 42,
        "format": "ebook",
        "classification": "REVIEW",
        "title": "Bel Canto",
        "author": "Ann Patchett",
        "stored_path": str(Path("/data/media/books") / relative),
        "local_path": str(read_root / relative),
    }
    return {
        "staged": staged,
        "admission_root": admission_root,
        "relative": relative,
        "result": result,
    }


def test_verified_snapshot_is_published_without_deleting_staging(admission_setup):
    client = FakeClient()
    setup = admission_setup

    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )

    destination = setup["admission_root"] / setup["relative"]
    assert response["status"] == "scan_requested"
    assert response["publicationMethod"] == "renameat2"
    assert response["stagingRetained"] is True
    assert client.scan_requests == 1
    assert setup["staged"].is_file()
    assert destination.is_file()
    assert destination.read_bytes() == setup["staged"].read_bytes()
    assert destination.stat().st_ino != setup["staged"].stat().st_ino
    assert destination.stat().st_mode & 0o777 == 0o644
    assert not list(destination.parent.glob(".bookguard-admission-*"))

    record = ebook_admission_by_id(response["admissionId"])
    assert record is not None
    assert record["status"] == "scan_requested"
    assert record["publication_method"] == "renameat2"
    assert record["staged_sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert record["verification"]["safeToAdmit"] is True


def test_historical_pass_result_can_be_restored(admission_setup):
    setup = admission_setup
    setup["result"]["classification"] = "PASS"

    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        FakeClient(),
    )

    assert response["status"] == "scan_requested"
    assert (setup["admission_root"] / setup["relative"]).is_file()


def test_wrong_snapshot_is_not_published(admission_setup):
    setup = admission_setup
    _write_epub(
        setup["staged"],
        "Death of a Texan",
        "Cat Hickey",
        "Death of a Texan by Cat Hickey. " + ("Different content. " * 80),
    )

    with pytest.raises(admission.AdmissionSafetyError, match="failed admission verification"):
        admission.admit_staged_ebook(
            setup["result"],
            setup["staged"].name,
            FakeClient(),
        )

    assert not (setup["admission_root"] / setup["relative"]).exists()


def test_existing_destination_is_never_overwritten(admission_setup):
    setup = admission_setup
    destination = setup["admission_root"] / setup["relative"]
    destination.write_bytes(b"keep me")

    with pytest.raises(admission.AdmissionSafetyError, match="already exists"):
        admission.admit_staged_ebook(
            setup["result"],
            setup["staged"].name,
            FakeClient(),
        )

    assert destination.read_bytes() == b"keep me"


def test_mismatched_root_mapping_is_rejected(admission_setup):
    setup = admission_setup
    setup["result"]["stored_path"] = "/different/root/book.epub"

    with pytest.raises(admission.AdmissionSafetyError, match="outside"):
        admission.admit_staged_ebook(
            setup["result"],
            setup["staged"].name,
            FakeClient(),
        )


def test_changed_book_identity_is_rejected(admission_setup):
    setup = admission_setup

    with pytest.raises(admission.AdmissionSafetyError, match="no longer matches"):
        admission.admit_staged_ebook(
            setup["result"],
            setup["staged"].name,
            FakeClient(title="Different Book"),
        )


def test_unstable_source_aborts_and_cleans_private_snapshot(
    admission_setup,
    monkeypatch,
):
    setup = admission_setup
    monkeypatch.setattr(admission, "_same_file_state", lambda left, right: False)

    with pytest.raises(admission.AdmissionSafetyError, match="changed while"):
        admission.admit_staged_ebook(
            setup["result"],
            setup["staged"].name,
            FakeClient(),
        )

    destination = setup["admission_root"] / setup["relative"]
    assert not destination.exists()
    assert not list(destination.parent.glob(".bookguard-admission-*"))


def test_symlinked_destination_directory_is_rejected(admission_setup, tmp_path):
    setup = admission_setup
    author_directory = setup["admission_root"] / "Ann Patchett"
    book_directory = author_directory / "Bel Canto (2001)"
    book_directory.rmdir()
    outside = tmp_path / "outside-library"
    outside.mkdir()
    book_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(admission.AdmissionSafetyError, match="Symlinked"):
        admission.admit_staged_ebook(
            setup["result"],
            setup["staged"].name,
            FakeClient(),
        )

    assert not (outside / setup["relative"].name).exists()


def test_scan_failure_keeps_verified_library_copy_for_recovery(admission_setup):
    setup = admission_setup
    client = FakeClient()
    client.scan_error = admission.BinderyClientError("Bindery unavailable")

    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )

    assert response["binderyScan"] == "request_failed"
    assert (setup["admission_root"] / setup["relative"]).is_file()
    record = ebook_admission_by_id(response["admissionId"])
    assert response["status"] == "scan_request_failed"
    assert record["status"] == "scan_request_failed"
    assert "unavailable" in record["error"]


def test_uncertain_scan_adopts_only_independently_proven_owner(
    admission_setup, monkeypatch,
):
    setup = admission_setup
    client = FakeClient()
    client.scan_error = admission.BinderyClientError("Bindery unavailable")
    response = admission.admit_staged_ebook(
        setup["result"], setup["staged"].name, client,
    )
    scans = client.scan_requests
    staged_hash = hashlib.sha256(setup["staged"].read_bytes()).hexdigest()
    published = setup["admission_root"] / setup["relative"]

    with pytest.raises(admission.AdmissionSafetyError, match="another scan is refused"):
        admission.reconcile_admission(
            response["admissionId"], client, allow_scan=False,
        )
    assert client.scan_requests == scans
    assert hashlib.sha256(published.read_bytes()).hexdigest() == staged_hash

    client.registered_path = setup["result"]["stored_path"]
    monkeypatch.setattr(admission, "associations_inside_path",
                        lambda stored_path: [])
    with pytest.raises(admission.AdmissionSafetyError, match="independently proven"):
        admission.reconcile_admission(
            response["admissionId"], client, allow_scan=False,
        )
    assert client.scan_requests == scans

    monkeypatch.setattr(admission, "associations_inside_path",
                        lambda stored_path: [{
                            "file_id": 91, "book_id": 42, "format": "ebook",
                            "stored_path": stored_path,
                        }])
    outcome = admission.reconcile_admission(
        response["admissionId"], client, allow_scan=False,
    )
    assert outcome["status"] == "registered"
    assert client.scan_requests == scans
    assert hashlib.sha256(published.read_bytes()).hexdigest() == staged_hash


def test_scan_failure_has_separate_proof_only_recovery_plan(admission_setup):
    from app.observe import _admission_decisions
    from app.recovery_planner import _build_plan

    setup = admission_setup
    client = FakeClient()
    client.scan_error = admission.BinderyClientError("Bindery unavailable")
    response = admission.admit_staged_ebook(
        setup["result"], setup["staged"].name, client,
    )
    with local_conn() as conn:
        decision = next(item for item in _admission_decisions(conn, 100)
                        if item["subjectId"] == str(response["admissionId"]))
        plan = _build_plan(conn, decision)

    assert decision["reasonCode"] == "REGISTRATION_SCAN_OUTCOME_UNKNOWN"
    assert plan["planKind"] == "RECONCILE_ADMISSION"
    assert plan["steps"][1]["code"] == "reconcile_known_admission"


def test_failed_scan_preview_requires_exact_registration_proof(
    admission_setup, monkeypatch,
):
    setup = admission_setup
    client = FakeClient()
    client.scan_error = admission.BinderyClientError("Bindery unavailable")
    response = admission.admit_staged_ebook(
        setup["result"], setup["staged"].name, client,
    )
    monkeypatch.setattr(admission, "result_by_id",
                        lambda _: dict(setup["result"]))
    monkeypatch.setattr(admission, "associations_inside_path",
                        lambda _: [])
    preview = admission.admission_reconcile_preview(response["admissionId"], client)
    assert preview["safe"] is True
    assert preview["scanRequestFailureProven"] is True
    assert preview["registrationState"] == "scan_required"

    client.registered_path = setup["result"]["stored_path"]
    monkeypatch.setattr(admission, "associations_inside_path",
                        lambda stored_path: [{
                            "file_id": 91, "book_id": 42, "format": "ebook",
                            "stored_path": stored_path,
                        }])
    preview = admission.admission_reconcile_preview(response["admissionId"], client)
    assert preview["safe"] is True
    assert preview["registrationState"] == "registered"


def test_reconcile_confirms_exact_bindery_path(admission_setup):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )
    client.registered_path = setup["result"]["stored_path"]

    reconciled = admission.reconcile_admission(response["admissionId"], client)

    assert reconciled["registered"] is True
    assert ebook_admission_by_id(response["admissionId"])["status"] == "registered"


def test_reconcile_stops_when_exact_path_belongs_to_wrong_book(
    admission_setup,
    monkeypatch,
):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )
    scans_before_reconcile = client.scan_requests
    monkeypatch.setattr(
        admission,
        "associations_inside_path",
        lambda stored_path: [
            {
                "file_id": 91,
                "book_id": 77,
                "format": "ebook",
                "stored_path": stored_path,
                "title": "A Different Book",
                "author": "Ann Patchett",
            }
        ],
    )

    reconciled = admission.reconcile_admission(response["admissionId"], client)

    assert reconciled["registered"] is False
    assert reconciled["status"] == "registration_conflict"
    assert reconciled["scanRequested"] is False
    assert reconciled["registrationConflict"]["expectedBookId"] == 42
    assert reconciled["registrationConflict"]["associations"][0]["book_id"] == 77
    assert client.scan_requests == scans_before_reconcile
    record = ebook_admission_by_id(response["admissionId"])
    assert record["status"] == "registration_conflict"
    assert "wrong book" in record["error"]

    repeated = admission.reconcile_admission(response["admissionId"], client)

    assert repeated["status"] == "registration_conflict"
    assert client.scan_requests == scans_before_reconcile


def test_registration_conflict_can_be_explicitly_rechecked_after_correction(
    admission_setup,
):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )
    update_ebook_admission(
        response["admissionId"],
        "registration_conflict",
        error="wrong owner",
    )
    client.registered_path = setup["result"]["stored_path"]

    reconciled = admission.reconcile_admission(response["admissionId"], client)

    assert reconciled["registered"] is True
    record = ebook_admission_by_id(response["admissionId"])
    assert record["status"] == "registered"
    assert record["error"] is None


def _prepare_registration_correction(
    admission_setup,
    monkeypatch,
    client,
):
    setup = admission_setup
    admitted = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )
    admission_id = admitted["admissionId"]
    update_ebook_admission(
        admission_id,
        "registration_conflict",
        error="wrong owner",
    )
    queue_id = 771
    client.queue_items = [
        {
            "id": queue_id,
            "bookId": setup["result"]["book_id"],
            "status": "importExternal",
        }
    ]
    owner = {
        "file_id": 91,
        "book_id": 77,
        "format": "ebook",
        "stored_path": setup["result"]["stored_path"],
        "title": "A Different Book",
        "author": "Ann Patchett",
    }
    owners = [owner]

    monkeypatch.setattr(
        admission,
        "associations_inside_path",
        lambda stored_path: [dict(item) for item in owners],
    )
    monkeypatch.setattr(
        admission,
        "result_by_id",
        lambda result_id: dict(setup["result"]),
    )
    monkeypatch.setattr(
        admission,
        "ebook_acquisition_by_admission_id",
        lambda requested_id: {
            "id": 31,
            "result_id": setup["result"]["id"],
            "book_id": setup["result"]["book_id"],
            "status": "admitted",
            "queue_id": queue_id,
            "admission_id": admission_id,
        },
    )

    def assign_to_target(tracked_path, target_book_id, file_format):
        owners[:] = [{
            **owner,
            "book_id": target_book_id,
            "title": setup["result"]["title"],
        }]

    client.on_reassign = assign_to_target
    return admission_id, owners, queue_id


def test_explicit_registration_correction_changes_only_bindery_ownership(
    admission_setup,
    monkeypatch,
):
    client = FakeClient()
    admission_id, owners, queue_id = _prepare_registration_correction(
        admission_setup,
        monkeypatch,
        client,
    )
    setup = admission_setup
    destination = setup["admission_root"] / setup["relative"]
    staged_before = setup["staged"].read_bytes()
    destination_before = destination.read_bytes()

    corrected = admission.correct_registration_conflict(admission_id, client)

    assert corrected["status"] == "registered"
    assert corrected["queueRecordRemoved"] is True
    assert corrected["removedFromDownloadClient"] is False
    assert corrected["downloadedDataDeleted"] is False
    assert corrected["libraryBytesChanged"] is False
    assert corrected["stagedFileRetained"] is True
    assert owners[0]["book_id"] == setup["result"]["book_id"]
    assert client.removed_queue_items == [(queue_id, False, False)]
    assert client.setting_updates == [
        ("import.mode", "auto"),
        ("import.mode", "external"),
    ]
    assert client.setting_values["import.mode"] == "external"
    assert setup["staged"].read_bytes() == staged_before
    assert destination.read_bytes() == destination_before
    assert ebook_admission_by_id(admission_id)["status"] == "registered"


def test_registration_correction_rejects_non_noop_preview(
    admission_setup,
    monkeypatch,
):
    client = FakeClient()
    admission_id, _, queue_id = _prepare_registration_correction(
        admission_setup,
        monkeypatch,
        client,
    )
    client.reassignment_preview = {
        "source": admission_setup["result"]["stored_path"],
        "destination": "/data/media/books/Somewhere/Else.epub",
        "format": "ebook",
        "status": "move",
    }

    with pytest.raises(
        admission.AdmissionSafetyError,
        match="not an exact no-move",
    ):
        admission.correct_registration_conflict(admission_id, client)

    assert client.removed_queue_items == []
    assert client.setting_updates == []
    assert client.queue_items[0]["id"] == queue_id
    assert ebook_admission_by_id(admission_id)["status"] == "registration_conflict"


def test_interrupted_registration_correction_is_durable_and_resumable(
    admission_setup,
    monkeypatch,
):
    client = FakeClient()
    admission_id, owners, queue_id = _prepare_registration_correction(
        admission_setup,
        monkeypatch,
        client,
    )
    client.reassignment_error = admission.BinderyClientError("simulated outage")

    with pytest.raises(
        admission.AdmissionSafetyError,
        match="did not complete safely",
    ):
        admission.correct_registration_conflict(admission_id, client)

    interrupted = ebook_admission_by_id(admission_id)
    assert interrupted["status"] == "registration_correcting"
    assert "retained" in interrupted["error"]
    assert client.removed_queue_items == [(queue_id, False, False)]
    assert client.queue_items == []
    assert client.setting_values["import.mode"] == "external"
    assert owners[0]["book_id"] == 77

    # Simulate a hard interruption after the temporary mode change. A retry
    # first restores external mode, then safely resumes with the known queue
    # record already absent.
    client.setting_values["import.mode"] = "auto"
    client.reassignment_error = None
    corrected = admission.correct_registration_conflict(admission_id, client)

    assert corrected["status"] == "registered"
    assert corrected["queueRecordRemoved"] is False
    assert client.setting_values["import.mode"] == "external"
    assert owners[0]["book_id"] == admission_setup["result"]["book_id"]
    assert ebook_admission_by_id(admission_id)["status"] == "registered"


def test_completed_reassignment_recovery_only_restores_mode_and_audit_state(
    admission_setup,
    monkeypatch,
):
    client = FakeClient()
    admission_id, owners, _ = _prepare_registration_correction(
        admission_setup,
        monkeypatch,
        client,
    )
    stored_path = admission_setup["result"]["stored_path"]
    target_book_id = admission_setup["result"]["book_id"]
    owners[0]["book_id"] = target_book_id
    owners[0]["title"] = admission_setup["result"]["title"]
    client.registered_path = stored_path
    client.queue_items = []
    client.setting_values["import.mode"] = "auto"
    update_ebook_admission(
        admission_id,
        "registration_correcting",
        error="simulated interruption after reassignment",
    )

    recovered = admission.correct_registration_conflict(admission_id, client)

    assert recovered["alreadyCorrected"] is True
    assert recovered["queueRecordRemoved"] is False
    assert client.setting_updates == [("import.mode", "external")]
    assert ebook_admission_by_id(admission_id)["status"] == "registered"


def test_reconcile_does_not_scan_when_path_ownership_is_unavailable(
    admission_setup,
    monkeypatch,
):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )
    scans_before_reconcile = client.scan_requests
    monkeypatch.setattr(
        admission,
        "associations_inside_path",
        lambda stored_path: (_ for _ in ()).throw(
            sqlite3.OperationalError("database unavailable")
        ),
    )

    with pytest.raises(
        admission.AdmissionSafetyError,
        match="ownership could not be confirmed",
    ):
        admission.reconcile_admission(response["admissionId"], client)

    assert client.scan_requests == scans_before_reconcile


def test_reconcile_refuses_failed_admission_record(admission_setup):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )
    update_ebook_admission(response["admissionId"], "failed", error="simulated failure")

    with pytest.raises(admission.AdmissionSafetyError, match="not eligible"):
        admission.reconcile_admission(response["admissionId"], client)

    assert ebook_admission_by_id(response["admissionId"])["status"] == "failed"


def test_reconcile_requires_actions_and_admission_to_remain_enabled(
    admission_setup,
    monkeypatch,
):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )

    monkeypatch.setattr(admission.settings, "allow_actions", False)
    with pytest.raises(admission.AdmissionSafetyError, match="Actions are disabled"):
        admission.reconcile_admission(response["admissionId"], client)

    monkeypatch.setattr(admission.settings, "allow_actions", True)
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ENABLED", "false")
    with pytest.raises(admission.AdmissionSafetyError, match="admission is disabled"):
        admission.reconcile_admission(response["admissionId"], client)

    assert client.scan_requests == 1


def test_post_publication_failure_retains_reconcilable_audit_state(
    admission_setup,
    monkeypatch,
):
    setup = admission_setup
    publish = admission._publish_no_replace

    def publish_then_fail(temporary, destination):
        method = publish(temporary, destination)
        raise admission.PublishedSnapshotError(
            method,
            OSError("simulated finalization failure"),
        )

    monkeypatch.setattr(admission, "_publish_no_replace", publish_then_fail)

    with pytest.raises(admission.AdmissionSafetyError, match="was published"):
        admission.admit_staged_ebook(
            setup["result"],
            setup["staged"].name,
            FakeClient(),
        )

    record = recent_ebook_admissions(1)[0]
    assert record["status"] == "published"
    assert record["publication_method"] == "renameat2"
    assert "finalization failed" in record["error"]
    assert (setup["admission_root"] / setup["relative"]).is_file()


def test_readiness_fails_closed_when_admission_disabled(admission_setup, monkeypatch):
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ENABLED", "false")

    result = admission.admission_readiness(FakeClient())

    assert result["ready"] is False
    assert "admissionEnabled" in result["blockers"]


def test_atomic_publish_refuses_to_replace_existing_file(tmp_path):
    private = tmp_path / ".bookguard-admission-test"
    private.mkdir(mode=0o700)
    temporary = private / "snapshot.epub"
    destination = tmp_path / "book.epub"
    temporary.write_bytes(b"new")
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        admission._publish_no_replace(temporary, destination)

    assert destination.read_bytes() == b"existing"
    assert temporary.read_bytes() == b"new"


def test_atomic_publish_falls_back_to_private_snapshot_link(tmp_path, monkeypatch):
    private = tmp_path / ".bookguard-admission-test"
    private.mkdir(mode=0o700)
    temporary = private / "snapshot.epub"
    destination = tmp_path / "book.epub"
    temporary.write_bytes(b"verified snapshot")

    monkeypatch.setattr(admission, "_rename_no_replace", lambda *args: False)

    method = admission._publish_no_replace(temporary, destination)

    assert method == "private-snapshot-link"
    assert destination.read_bytes() == b"verified snapshot"
    assert destination.stat().st_nlink == 1
    assert not temporary.exists()
    assert not private.exists()


def test_unsupported_no_replace_records_verified_failure_stage(
    admission_setup, monkeypatch,
):
    setup = admission_setup
    monkeypatch.setattr(admission, "_rename_no_replace", lambda *args: False)
    monkeypatch.setattr(admission.os, "link", lambda *args, **kwargs: (
        _ for _ in ()).throw(OSError(errno.EINVAL, "Invalid argument")))

    with pytest.raises(admission.AdmissionSafetyError, match="Invalid argument"):
        admission.admit_staged_ebook(
            setup["result"], setup["staged"].name, FakeClient(),
        )

    records = recent_ebook_admissions()
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["failure_stage"] == "no_replace_unsupported"
    assert records[0]["staged_sha256"]
    assert records[0]["verification"]
    assert not (setup["admission_root"] / setup["relative"]).exists()


def test_existing_admission_table_gains_publication_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(admission.settings, "config_dir", str(tmp_path))
    database = tmp_path / "bookguard.db"
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE ebook_admissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id INTEGER NOT NULL,
                scan_id TEXT NOT NULL,
                book_id INTEGER NOT NULL,
                staged_relative_path TEXT NOT NULL,
                staged_sha256 TEXT,
                stored_path TEXT NOT NULL,
                local_path TEXT NOT NULL,
                status TEXT NOT NULL,
                verification_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT
            )
        """)

    init_local_db()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(ebook_admissions)"
            ).fetchall()
        }
    assert "publication_method" in columns
    assert "failure_stage" in columns



def test_admission_reconcile_preview_allows_only_consistent_scan_pending_state(
    admission_setup,
    monkeypatch,
):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )

    monkeypatch.setattr(
        admission,
        "result_by_id",
        lambda result_id: dict(setup["result"]),
    )
    monkeypatch.setattr(
        admission,
        "associations_inside_path",
        lambda stored_path: [],
    )

    preview = admission.admission_reconcile_preview(
        response["admissionId"],
        client,
    )

    assert preview["safe"] is True
    assert preview["status"] == "scan_requested"
    assert preview["registrationState"] == "scan_required"
    assert preview["checks"]["resultIdentityUnchanged"] is True
    assert preview["checks"]["publishedBytesCurrent"] is True
    assert preview["checks"]["stagedBytesCurrent"] is True
    assert preview["checks"]["binderyOwnershipConsistent"] is True


def test_admission_reconcile_preview_requires_api_and_database_owner_agreement(
    admission_setup,
    monkeypatch,
):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )
    client.registered_path = setup["result"]["stored_path"]

    monkeypatch.setattr(
        admission,
        "result_by_id",
        lambda result_id: dict(setup["result"]),
    )
    monkeypatch.setattr(
        admission,
        "associations_inside_path",
        lambda stored_path: [],
    )

    preview = admission.admission_reconcile_preview(
        response["admissionId"],
        client,
    )

    assert preview["safe"] is False
    assert preview["registrationState"] == "inconsistent"
    assert preview["checks"]["binderyOwnershipConsistent"] is False


def test_admission_reconcile_preview_accepts_exact_registered_owner(
    admission_setup,
    monkeypatch,
):
    setup = admission_setup
    client = FakeClient()
    response = admission.admit_staged_ebook(
        setup["result"],
        setup["staged"].name,
        client,
    )
    client.registered_path = setup["result"]["stored_path"]

    monkeypatch.setattr(
        admission,
        "result_by_id",
        lambda result_id: dict(setup["result"]),
    )
    monkeypatch.setattr(
        admission,
        "associations_inside_path",
        lambda stored_path: [{
            "file_id": 91,
            "book_id": setup["result"]["book_id"],
            "format": "ebook",
            "stored_path": stored_path,
            "title": setup["result"]["title"],
            "author": setup["result"]["author"],
        }],
    )

    preview = admission.admission_reconcile_preview(
        response["admissionId"],
        client,
    )

    assert preview["safe"] is True
    assert preview["registrationState"] == "registered"
    assert preview["checks"]["binderyOwnershipConsistent"] is True
