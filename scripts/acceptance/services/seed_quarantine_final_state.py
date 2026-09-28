#!/usr/bin/env python3
"""Seed an already finalized disposable child for proof-only parent acceptance."""

import json
from pathlib import Path
import shutil
import sqlite3

from app.db import ebook_replacement_for_quarantine_plan, local_conn, result_by_id
from app.file_safety import sha256_file


def main() -> None:
    child = ebook_replacement_for_quarantine_plan(1)
    assert child and child["status"] == "verified" and child["admission_id"] is None
    result = result_by_id(int(child["result_id"]))
    assert result and int(result["book_id"]) == 101
    assert result["stored_path"] == "/data/media/books/BookGuard Test/Conflict Fixture.epub"
    staged = Path("/staging") / child["staged_relative_path"]
    published = Path(result["local_path"])
    assert staged.is_file() and not staged.is_symlink() and not published.exists()
    assert sha256_file(staged) == child["staged_sha256"]
    shutil.copyfile(staged, published)
    assert sha256_file(published) == child["staged_sha256"]
    staged.unlink()

    with sqlite3.connect("/state/bindery.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM book_files WHERE path=?",
                            (result["stored_path"],)).fetchone()[0] == 0
        conn.execute(
            "INSERT INTO book_files(id, book_id, format, path) VALUES(9003, 101, 'ebook', ?)",
            (result["stored_path"],),
        )
    state_file = Path("/state/state.json")
    state = json.loads(state_file.read_text())
    assert len(state["queue"]) == 1 and state["queue"][0]["id"] == child["queue_id"]
    state["queue"] = []
    state_file.write_text(json.dumps(state, sort_keys=True))

    with local_conn() as conn:
        admission_id = conn.execute(
            """INSERT INTO ebook_admissions(
                 result_id, scan_id, book_id, staged_relative_path, staged_sha256,
                 stored_path, local_path, status, publication_method, created_at, updated_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, 'registered', 'link',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (child["result_id"], child["scan_id"], child["book_id"],
             child["staged_relative_path"], child["staged_sha256"],
             result["stored_path"], result["local_path"]),
        ).lastrowid
        conn.execute(
            "UPDATE ebook_acquisitions SET status='finalized', admission_id=?, queue_status='absent' WHERE id=?",
            (admission_id, child["id"]),
        )
        conn.commit()
    print(json.dumps({"replacementAcquisitionId": child["id"],
                      "admissionId": admission_id, "sha256": child["staged_sha256"]}))


if __name__ == "__main__":
    main()
