#!/usr/bin/env python3
"""Seed one disposable failed 409 grab without any ebook bytes."""

from app.db import (
    add_result, create_ebook_acquisition, create_scan, finish_scan,
    init_local_db, local_conn, update_ebook_acquisition,
)


init_local_db()
create_scan("alternate-acceptance-scan", 1)
add_result("alternate-acceptance-scan", {
    "file_id": 501, "book_id": 101, "author": "Fixture Author",
    "title": "Alternate Fixture", "format": "ebook",
    "stored_path": "/data/media/books/Alternate Fixture.epub",
    "local_path": "/books/Alternate Fixture.epub",
    "classification": "REVIEW", "risk_score": 50,
    "reason_code": "ALTERNATE_ACCEPTANCE", "reasons": ["disposable fixture"],
    "metadata": {},
})
finish_scan("alternate-acceptance-scan")
with local_conn() as conn:
    result = dict(conn.execute(
        "SELECT * FROM scan_results WHERE scan_id=? LIMIT 1",
        ("alternate-acceptance-scan",),
    ).fetchone())
parent_id = create_ebook_acquisition(result, {
    "guid": "rejected-guid", "title": "Fixture Author - Alternate Fixture rejected",
    "protocol": "usenet", "indexerName": "Disposable Indexer",
})
update_ebook_acquisition(
    parent_id, "failed",
    error=(
        "Bindery grab failed: Bindery POST /queue/grab returned HTTP 409: "
        "already grabbed: this release has already been imported"
    ),
)
print(f"Seeded disposable failed acquisition {parent_id} without media bytes.")
