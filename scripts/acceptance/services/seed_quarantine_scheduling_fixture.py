#!/usr/bin/env python3
"""Add an independent unsafe book after the standard disposable quarantine fixture."""

from pathlib import Path
import sqlite3
import zipfile

from app.db import add_result, local_conn
from app.verifier import verify_result
from seed_unsafe_quarantine_fixture import main as seed_first


RELATIVE = "Second/Second Unsafe.epub"
STORED = "/data/media/books/" + RELATIVE


def main() -> None:
    seed_first()
    source = Path("/books") / RELATIVE
    source.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("../second-unsafe.txt", "second disposable unsafe archive")
    add_result("unsafe-quarantine-acceptance", {
        "file_id": 9003, "book_id": 303, "author": "Conflict Author",
        "title": "Second Unsafe", "format": "ebook", "stored_path": STORED,
        "local_path": str(source), "classification": "REVIEW", "risk_score": 50,
        "reason_code": "SECOND_UNSAFE_QUARANTINE_ACCEPTANCE",
        "reasons": [], "metadata": {},
    })
    with local_conn() as conn:
        result = dict(conn.execute(
            "SELECT * FROM scan_results WHERE file_id=9003",
        ).fetchone())
        conn.execute(
            "UPDATE scans SET total=2, processed=2 WHERE id=?",
            ("unsafe-quarantine-acceptance",),
        )
        conn.commit()
    verification = verify_result(result, force=True)
    assert verification["verdict"] == "UNSAFE_FILE"
    assert verification["source"] == "deterministic-safety"
    with sqlite3.connect("/bindery/bindery.db") as conn:
        conn.execute(
            "INSERT INTO books(id, author_id, title, status) "
            "VALUES(303, 1, 'Second Unsafe', 'wanted')"
        )
        conn.execute(
            "INSERT INTO book_files(id, book_id, format, path) VALUES(9003, 303, 'ebook', ?)",
            (STORED,),
        )
    print("Seeded two independent disposable unsafe books and one unrelated control.")


if __name__ == "__main__":
    main()
