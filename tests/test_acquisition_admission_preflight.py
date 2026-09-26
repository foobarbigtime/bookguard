from __future__ import annotations

from pathlib import Path
import os
import zipfile

import pytest

import app.acquisition_admission_preflight as preflight
from app.acquisition_admission_preflight import acquisition_admission_preview
from app.config import settings
from app.db import (
    add_result, create_ebook_acquisition, create_scan, finish_scan,
    init_local_db, local_conn, update_ebook_acquisition,
)
from app.observe import _acquisition_decisions
from app.recovery_planner import record_recovery_plans
from app.staging import verify_staged_ebook


class FakeClient:
    def __init__(self):
        self.queue_status = "importExternal"
        self.registered = False

    def list_queue(self):
        return {"items": [{
            "id": 77, "bookId": 101, "title": "Review Fixture release",
            "protocol": "usenet", "status": self.queue_status,
        }], "partial": False}

    def get_book(self, book_id):
        return {
            "id": book_id, "title": "Review Fixture", "authorName": "Fixture Author",
            "ebookFilePath": "/data/media/books/Review Fixture.epub" if self.registered else "",
            "bookFiles": [],
        }

    def get_setting(self, key):
        return {
            "import.mode": "external",
            "import.drop_folder": "/data/bookguard-staging",
            "import.drop_layout": "flat",
            "import.drop_link_mode": "copy",
        }[key]


def _write_epub(path: Path) -> None:
    container = (
        '<?xml version="1.0"?><container '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    package = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>Review Fixture</dc:title><dc:creator>Fixture Author</dc:creator>'
        '<dc:identifier>review-acceptance</dc:identifier></metadata>'
        '<manifest><item id="chapter" href="chapter.xhtml" '
        'media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="chapter"/></spine></package>'
    )
    body = "Review Fixture by Fixture Author. " + ("A fictional passage. " * 100)
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<h1>Review Fixture</h1><p>by Fixture Author</p><p>'
        + body + "</p></body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    books = tmp_path / "books"
    admission = tmp_path / "admission"
    quarantine = tmp_path / "quarantine"
    for path in (staging, books, admission, quarantine):
        path.mkdir()
    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ROOT", str(admission))
    monkeypatch.setenv("BOOKGUARD_ADMISSION_BINDERY_ROOT", "/data/media/books")
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/data/bookguard-staging")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ENABLED", "true")
    monkeypatch.setattr(settings, "allow_actions", True)
    monkeypatch.setattr(settings, "ebook_root", str(books))
    monkeypatch.setattr(settings, "ebook_bindery_prefix", "/data/media/books")
    monkeypatch.setattr(settings, "quarantine_root", str(quarantine))
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    init_local_db()
    create_scan("review-preflight-scan", 1)
    add_result("review-preflight-scan", {
        "file_id": 501, "book_id": 101, "author": "Fixture Author",
        "title": "Review Fixture", "format": "ebook",
        "stored_path": "/data/media/books/Review Fixture.epub",
        "local_path": str(books / "Review Fixture.epub"),
        "classification": "REVIEW", "risk_score": 50,
        "reason_code": "REVIEW_FIXTURE", "reasons": ["fixture"], "metadata": {},
    })
    finish_scan("review-preflight-scan")
    with local_conn() as conn:
        result = dict(conn.execute(
            "SELECT * FROM scan_results WHERE scan_id='review-preflight-scan'"
        ).fetchone())
    client = FakeClient()
    staged = staging / "Review Fixture.epub"
    _write_epub(staged)
    verification = verify_staged_ebook(101, staged.name, client)
    assert verification["safeToAdmit"] is True
    acquisition_id = create_ebook_acquisition(result, {
        "guid": "review-guid", "title": "Review Fixture release",
        "indexerName": "Fixture", "protocol": "usenet",
    })
    update_ebook_acquisition(
        acquisition_id, "verified", queue_id=77, queue_status="importexternal",
        staged_relative_path=staged.name, staged_sha256=verification["sha256"],
        verification=verification,
    )
    with local_conn() as conn:
        record_recovery_plans(conn, _acquisition_decisions(conn, 100))
        conn.commit()
    return acquisition_id, client, staged, admission / staged.name


def _assert_no_admission(destination):
    with local_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ebook_admissions").fetchone()[0] == 0
    assert not destination.exists()


def test_preview_proves_current_evidence_without_publication(prepared):
    acquisition_id, client, staged, destination = prepared
    before = staged.read_bytes()

    preview = acquisition_admission_preview(acquisition_id, client)

    assert preview["ok"] is True
    assert preview["readOnly"] is True
    assert preview["admissionAttempted"] is False
    assert staged.read_bytes() == before
    _assert_no_admission(destination)


@pytest.mark.parametrize("change,reason", [
    ("changed_bytes", "STAGED_BYTES_UNPROVEN"),
    ("unfinished_queue", "QUEUE_HANDOFF_UNPROVEN"),
    ("registered_book", "BINDERY_BOOK_CHANGED"),
    ("occupied_destination", "DEPENDENCY_OR_IDENTITY_UNPROVEN"),
])
def test_preview_blocks_changed_boundary_without_publication(prepared, change, reason):
    acquisition_id, client, staged, destination = prepared
    if change == "changed_bytes":
        staged.write_bytes(staged.read_bytes() + b"changed")
    elif change == "unfinished_queue":
        client.queue_status = "downloading"
    elif change == "registered_book":
        client.registered = True
    else:
        destination.write_bytes(b"occupied")

    preview = acquisition_admission_preview(acquisition_id, client)

    assert preview["ok"] is False
    assert preview["reasonCode"] == reason
    assert preview["admissionAttempted"] is False
    with local_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ebook_admissions").fetchone()[0] == 0


def test_preview_blocks_same_bytes_replaced_during_verification(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    original = preflight.verify_staged_ebook

    def replace_source(*args):
        verified = original(*args)
        replacement = staged.with_suffix(".replacement")
        replacement.write_bytes(staged.read_bytes())
        os.replace(replacement, staged)
        return verified

    monkeypatch.setattr(preflight, "verify_staged_ebook", replace_source)
    preview = acquisition_admission_preview(acquisition_id, client)

    assert preview["reasonCode"] == "STAGED_BYTES_CHANGED"
    _assert_no_admission(destination)


@pytest.mark.parametrize("change", ["new_admission", "superseded_plan", "changed_acquisition"])
def test_preview_blocks_durable_change_during_verification(
    prepared, monkeypatch, change,
):
    acquisition_id, client, staged, destination = prepared
    original = preflight.verify_staged_ebook

    def change_durable_state(*args):
        verified = original(*args)
        with local_conn() as conn:
            if change == "new_admission":
                conn.execute(
                    """INSERT INTO ebook_admissions(
                         result_id, scan_id, book_id, staged_relative_path,
                         stored_path, local_path, status, created_at, updated_at
                       ) SELECT result_id, scan_id, book_id, staged_relative_path,
                         '/data/media/books/Review Fixture.epub',
                         '/books/Review Fixture.epub', 'preparing',
                         datetime('now'), datetime('now')
                         FROM ebook_acquisitions WHERE id=?""",
                    (acquisition_id,),
                )
            elif change == "superseded_plan":
                conn.execute(
                    "UPDATE recovery_plans SET state='superseded' WHERE subject_id=?",
                    (str(acquisition_id),),
                )
            else:
                conn.execute(
                    "UPDATE ebook_acquisitions SET queue_status='downloading' WHERE id=?",
                    (acquisition_id,),
                )
            conn.commit()
        return verified

    monkeypatch.setattr(preflight, "verify_staged_ebook", change_durable_state)
    preview = acquisition_admission_preview(acquisition_id, client)

    assert preview["reasonCode"] == "DURABLE_STATE_CHANGED"
    assert not destination.exists()
