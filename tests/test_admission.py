from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

import app.admission as admission
import app.ebook_extraction as ebook_extraction
from app.db import ebook_admission_by_id, init_local_db


class FakeClient:
    def __init__(self, *, title: str = "Bel Canto", author: str = "Ann Patchett"):
        self.title = title
        self.author = author
        self.registered_path = ""
        self.scan_requests = 0
        self.scan_error: Exception | None = None

    def get_setting(self, key: str):
        return {
            "import.mode": "external",
            "import.drop_folder": "/data/bookguard-staging",
            "import.drop_layout": "flat",
            "import.drop_link_mode": "copy",
        }[key]

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
    assert record["staged_sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert record["verification"]["safeToAdmit"] is True


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
    assert record["status"] == "published"
    assert "unavailable" in record["error"]


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
