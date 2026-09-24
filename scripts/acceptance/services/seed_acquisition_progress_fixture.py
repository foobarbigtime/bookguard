#!/usr/bin/env python3
"""Seed a disposable queued acquisition; no library ebook is published."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from app.db import (
    add_result, create_ebook_acquisition, create_scan, finish_scan,
    init_local_db, local_conn, update_ebook_acquisition,
)


STAGED = "Progress Fixture.epub"


def write_epub(path: Path) -> None:
    container = (
        '<?xml version="1.0"?><container '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    package = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>Progress Fixture</dc:title><dc:creator>Fixture Author</dc:creator>'
        '<dc:identifier>progress-acceptance</dc:identifier></metadata>'
        '<manifest><item id="chapter" href="chapter.xhtml" '
        'media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="chapter"/></spine></package>'
    )
    body = "Progress Fixture by Fixture Author. " + ("A fictional passage. " * 100)
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<h1>Progress Fixture</h1><p>by Fixture Author</p><p>'
        + body + "</p></body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


def main() -> None:
    init_local_db()
    staged = Path("/staging") / STAGED
    write_epub(staged)
    create_scan("progress-acceptance-scan", 1)
    add_result("progress-acceptance-scan", {
        "file_id": 501, "book_id": 101, "author": "Fixture Author",
        "title": "Progress Fixture", "format": "ebook",
        "stored_path": "/data/media/books/Progress Fixture.epub",
        "local_path": "/books/Progress Fixture.epub",
        "classification": "REVIEW", "risk_score": 50,
        "reason_code": "PROGRESS_ACCEPTANCE",
        "reasons": ["disposable acceptance fixture"], "metadata": {},
    })
    finish_scan("progress-acceptance-scan")
    with local_conn() as conn:
        result = dict(conn.execute(
            "SELECT * FROM scan_results WHERE scan_id=? LIMIT 1",
            ("progress-acceptance-scan",),
        ).fetchone())
    acquisition_id = create_ebook_acquisition(result, {
        "guid": "progress-acceptance-guid",
        "title": "Progress Fixture release",
        "indexerName": "BookGuard Acceptance", "protocol": "usenet",
    })
    update_ebook_acquisition(
        acquisition_id, "queued", queue_id=77, queue_status="queued"
    )
    print(json.dumps({
        "acquisitionId": acquisition_id,
        "stagedRelativePath": STAGED,
        "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
