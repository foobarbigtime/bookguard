#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
FILE_ID = 501
QUEUE_ID = 77
STAGED_RELATIVE = "BookGuard Test/Finalization Fixture.epub"
STORED_PATH = "/data/media/books/BookGuard Test/Finalization Fixture.epub"
LOCAL_PATH = "/books/BookGuard Test/Finalization Fixture.epub"
FIXTURE_BYTES = b"BookGuard disposable finalization acceptance fixture\n"


def main() -> None:
    init_local_db()

    staged = Path("/staging") / STAGED_RELATIVE
    library = Path(LOCAL_PATH)
    staged.parent.mkdir(parents=True, exist_ok=True)
    library.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(FIXTURE_BYTES)
    library.write_bytes(FIXTURE_BYTES)
    digest = hashlib.sha256(FIXTURE_BYTES).hexdigest()

    create_scan("finalization-acceptance-scan", 1)
    add_result(
        "finalization-acceptance-scan",
        {
            "file_id": FILE_ID,
            "book_id": BOOK_ID,
            "author": "Finalization Author",
            "title": "Finalization Fixture",
            "format": "ebook",
            "stored_path": STORED_PATH,
            "local_path": LOCAL_PATH,
            "classification": "REVIEW",
            "risk_score": 50,
            "reason_code": "FINALIZATION_ACCEPTANCE",
            "reasons": ["disposable acceptance fixture"],
            "metadata": {},
        },
    )
    finish_scan("finalization-acceptance-scan")

    with local_conn() as conn:
        result = dict(
            conn.execute(
                "SELECT * FROM scan_results WHERE scan_id=? LIMIT 1",
                ("finalization-acceptance-scan",),
            ).fetchone()
        )

    acquisition_id = create_ebook_acquisition(
        result,
        {
            "guid": "finalization-acceptance-guid",
            "title": "Finalization Fixture release",
            "indexerName": "BookGuard Acceptance",
            "protocol": "usenet",
        },
    )
    admission_id = create_ebook_admission(result, STAGED_RELATIVE)
    update_ebook_admission(
        admission_id,
        "registered",
        staged_sha256=digest,
        publication_method="acceptance-fixture",
    )
    update_ebook_acquisition(
        acquisition_id,
        "admitted",
        queue_id=QUEUE_ID,
        queue_status="imported",
        staged_relative_path=STAGED_RELATIVE,
        staged_sha256=digest,
        admission_id=admission_id,
    )

    print(
        json.dumps(
            {
                "configDir": settings.config_dir,
                "acquisitionId": acquisition_id,
                "admissionId": admission_id,
                "resultId": int(result["id"]),
                "bookId": BOOK_ID,
                "queueId": QUEUE_ID,
                "stagedRelativePath": STAGED_RELATIVE,
                "sha256": digest,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
