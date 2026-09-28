#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from app.config import settings
from app.db import (
    add_result,
    create_ebook_acquisition,
    create_ebook_admission,
    create_scan,
    finish_scan,
    init_local_db,
    local_conn,
    update_ebook_acquisition,
    update_ebook_admission,
)


BOOK_ID = 101
FOREIGN_BOOK_ID = 202
FILE_ID = 501
BINDERY_FILE_ID = 9001
QUEUE_ID = 77
STAGED_RELATIVE = "BookGuard Test/Conflict Fixture.epub"
STORED_PATH = "/data/media/books/BookGuard Test/Conflict Fixture.epub"
LOCAL_PATH = "/admission-books/BookGuard Test/Conflict Fixture.epub"
FIXTURE_BYTES = b"BookGuard disposable registration conflict acceptance fixture\n"


def seed_bindery_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS book_files;
            DROP TABLE IF EXISTS books;
            DROP TABLE IF EXISTS authors;
            CREATE TABLE authors (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                author_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'wanted'
            );
            CREATE TABLE book_files (
                id INTEGER PRIMARY KEY,
                book_id INTEGER NOT NULL,
                format TEXT NOT NULL,
                path TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO authors(id, name) VALUES(1, ?)", ("Conflict Author",))
        conn.execute("INSERT INTO authors(id, name) VALUES(2, ?)", ("Foreign Author",))
        conn.execute(
            "INSERT INTO books(id, author_id, title, status) VALUES(?, 1, ?, 'wanted')",
            (BOOK_ID, "Conflict Fixture"),
        )
        conn.execute(
            "INSERT INTO books(id, author_id, title, status) VALUES(?, 2, ?, 'wanted')",
            (FOREIGN_BOOK_ID, "Foreign Fixture"),
        )
        conn.execute(
            "INSERT INTO book_files(id, book_id, format, path) VALUES(?, ?, 'ebook', ?)",
            (BINDERY_FILE_ID, FOREIGN_BOOK_ID, STORED_PATH),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    init_local_db()

    staged = Path("/staging") / STAGED_RELATIVE
    published = Path(LOCAL_PATH)
    staged.parent.mkdir(parents=True, exist_ok=True)
    published.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(FIXTURE_BYTES)
    published.write_bytes(FIXTURE_BYTES)
    digest = hashlib.sha256(FIXTURE_BYTES).hexdigest()

    create_scan("registration-conflict-acceptance-scan", 1)
    add_result(
        "registration-conflict-acceptance-scan",
        {
            "file_id": FILE_ID,
            "book_id": BOOK_ID,
            "author": "Conflict Author",
            "title": "Conflict Fixture",
            "format": "ebook",
            "stored_path": STORED_PATH,
            "local_path": LOCAL_PATH,
            "classification": "REVIEW",
            "risk_score": 50,
            "reason_code": "REGISTRATION_CONFLICT_ACCEPTANCE",
            "reasons": ["disposable acceptance fixture"],
            "metadata": {},
        },
    )
    finish_scan("registration-conflict-acceptance-scan")

    with local_conn() as conn:
        result = dict(
            conn.execute(
                "SELECT * FROM scan_results WHERE scan_id=? LIMIT 1",
                ("registration-conflict-acceptance-scan",),
            ).fetchone()
        )

    acquisition_id = create_ebook_acquisition(
        result,
        {
            "guid": "registration-conflict-guid",
            "title": "Conflict Fixture release",
            "indexerName": "BookGuard Acceptance",
            "protocol": "usenet",
        },
    )
    admission_id = create_ebook_admission(result, STAGED_RELATIVE)
    update_ebook_admission(
        admission_id,
        "registration_conflict",
        staged_sha256=digest,
        publication_method="acceptance-fixture",
        error="exact path belongs to the wrong Bindery book",
    )
    update_ebook_acquisition(
        acquisition_id,
        "admitted",
        queue_id=QUEUE_ID,
        queue_status="importexternal",
        staged_relative_path=STAGED_RELATIVE,
        staged_sha256=digest,
        admission_id=admission_id,
    )

    seed_bindery_db(Path("/bindery/bindery.db"))

    print(
        json.dumps(
            {
                "configDir": settings.config_dir,
                "acquisitionId": acquisition_id,
                "admissionId": admission_id,
                "resultId": int(result["id"]),
                "bookId": BOOK_ID,
                "foreignBookId": FOREIGN_BOOK_ID,
                "queueId": QUEUE_ID,
                "stagedRelativePath": STAGED_RELATIVE,
                "storedPath": STORED_PATH,
                "sha256": digest,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
