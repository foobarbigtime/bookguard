#!/usr/bin/env python3
"""Seed a disposable, verified admission with no published ebook or Bindery owner."""

import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

from app.db import (
    add_result, create_ebook_admission, create_scan, finish_scan, init_local_db,
    local_conn, update_ebook_admission,
)
from seed_registration_conflict_fixture import seed_bindery_db


STORED = "/data/media/books/BookGuard Test/Conflict Fixture.epub"
STAGED = "BookGuard Test/Conflict Fixture.epub"


def write_epub(source: Path) -> None:
    container = '''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    package = '''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Conflict Fixture</dc:title>
    <dc:creator>Conflict Author</dc:creator>
    <dc:identifier>bookguard-disposable-acceptance</dc:identifier>
  </metadata>
  <manifest><item id="chapter" href="chapter.xhtml"
    media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chapter"/></spine>
</package>'''
    chapter = '''<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Conflict Fixture</h1><p>by Conflict Author</p>
<p>Disposable BookGuard acceptance passage. ''' + (
        'Only test bytes may be published. ' * 80
    ) + '</p></body></html>'
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


def main() -> None:
    init_local_db()
    source = Path("/staging") / STAGED
    destination = Path("/admission-books") / STAGED
    source.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_epub(source)
    assert not destination.exists()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    create_scan("publication-proof-acceptance", 1)
    add_result("publication-proof-acceptance", {
        "file_id": 501, "book_id": 101, "author": "Conflict Author",
        "title": "Conflict Fixture", "format": "ebook", "stored_path": STORED,
        "local_path": str(destination), "classification": "REVIEW",
        "risk_score": 0, "reason_code": "DISPOSABLE_ACCEPTANCE",
        "reasons": [], "metadata": {},
    })
    finish_scan("publication-proof-acceptance")
    with local_conn() as conn:
        result = dict(conn.execute(
            "SELECT * FROM scan_results WHERE scan_id=?",
            ("publication-proof-acceptance",),
        ).fetchone())
    admission_id = create_ebook_admission(result, STAGED)
    update_ebook_admission(
        admission_id, "failed", staged_sha256=digest,
        verification={"safeToAdmit": True, "sha256": digest},
        failure_stage="no_replace_unsupported",
        error="[Errno 95] Operation not supported: disposable proof fixture",
    )
    db_path = Path("/bindery/bindery.db")
    seed_bindery_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM book_files")
    print(json.dumps({"admissionId": admission_id, "sha256": digest}, sort_keys=True))


if __name__ == "__main__":
    main()
