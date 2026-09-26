#!/usr/bin/env python3
"""Seed one verified disposable acquisition and an unowned Bindery path."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from app.db import (
    add_result, create_ebook_acquisition, create_scan, finish_scan,
    init_local_db, local_conn, update_ebook_acquisition,
)
from app.staging import verify_ebook_file
from seed_publication_proof_fixture import write_epub
from seed_registration_conflict_fixture import seed_bindery_db


STAGED = "BookGuard Test/Conflict Fixture.epub"
STORED = "/data/media/books/" + STAGED


def main() -> None:
    init_local_db()
    source = Path("/staging") / STAGED
    destination = Path("/admission-books") / STAGED
    source.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_epub(source)
    assert not destination.exists()
    book = {
        "id": 101, "title": "Conflict Fixture",
        "authorName": "Conflict Author", "ebookFilePath": "", "bookFiles": [],
    }
    verification = verify_ebook_file(101, source, display_path=STAGED, book=book)
    assert verification["safeToAdmit"] is True

    create_scan("verified-admission-acceptance", 1)
    add_result("verified-admission-acceptance", {
        "file_id": 501, "book_id": 101, "author": "Conflict Author",
        "title": "Conflict Fixture", "format": "ebook", "stored_path": STORED,
        "local_path": str(destination), "classification": "REVIEW",
        "risk_score": 0, "reason_code": "DISPOSABLE_ACCEPTANCE",
        "reasons": [], "metadata": {},
    })
    finish_scan("verified-admission-acceptance")
    with local_conn() as conn:
        result = dict(conn.execute(
            "SELECT * FROM scan_results WHERE scan_id=?",
            ("verified-admission-acceptance",),
        ).fetchone())
    acquisition_id = create_ebook_acquisition(result, {
        "guid": "verified-admission-fixture", "title": "Conflict Fixture release",
        "indexerName": "BookGuard Acceptance", "protocol": "usenet",
    })
    update_ebook_acquisition(
        acquisition_id, "verified", queue_id=77, queue_status="importexternal",
        staged_relative_path=STAGED, staged_sha256=verification["sha256"],
        verification=verification,
    )

    db_path = Path("/bindery/bindery.db")
    seed_bindery_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM book_files")
    print(json.dumps({
        "acquisitionId": acquisition_id, "sha256": verification["sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
