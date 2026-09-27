#!/usr/bin/env python3
"""Seed one deterministically unsafe ebook and an unrelated disposable sibling."""

import json
from pathlib import Path
import sqlite3
import zipfile

from app.db import add_result, create_scan, finish_scan, init_local_db, local_conn
from app.verifier import verify_result
from seed_registration_conflict_fixture import seed_bindery_db


RELATIVE = "BookGuard Test/Conflict Fixture.epub"
STORED = "/data/media/books/" + RELATIVE
UNRELATED = "/data/media/books/Other/Unrelated.epub"


def main() -> None:
    init_local_db()
    source = Path("/books") / RELATIVE
    source.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("../unsafe-member.txt", "disposable archive traversal fixture")
    sibling = Path("/books/Other/Unrelated.epub")
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_bytes(b"Unrelated disposable media must remain unchanged.\n")

    create_scan("unsafe-quarantine-acceptance", 1)
    add_result("unsafe-quarantine-acceptance", {
        "file_id": 9001, "book_id": 101, "author": "Conflict Author",
        "title": "Conflict Fixture", "format": "ebook", "stored_path": STORED,
        "local_path": str(source), "classification": "REVIEW", "risk_score": 100,
        "reason_code": "UNSAFE_QUARANTINE_ACCEPTANCE", "reasons": [], "metadata": {},
    })
    finish_scan("unsafe-quarantine-acceptance")
    with local_conn() as conn:
        result = dict(conn.execute(
            "SELECT * FROM scan_results WHERE scan_id=?",
            ("unsafe-quarantine-acceptance",),
        ).fetchone())
    verification = verify_result(result, force=True)
    assert verification["verdict"] == "UNSAFE_FILE"
    assert verification["confidence"] == 100
    assert verification["source"] == "deterministic-safety"
    assert verification["evidence"]["security"]["checks"]["archiveSafety"]["status"] == "failed"

    db_path = Path("/bindery/bindery.db")
    seed_bindery_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE book_files SET book_id=101 WHERE id=9001")
        conn.execute(
            "INSERT INTO book_files(id, book_id, format, path) VALUES(9002, 202, 'ebook', ?)",
            (UNRELATED,),
        )
    print(json.dumps({"resultId": result["id"], "verdict": verification["verdict"]}))


if __name__ == "__main__":
    main()
