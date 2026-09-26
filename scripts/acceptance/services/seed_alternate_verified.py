#!/usr/bin/env python3
"""Independently verify disposable alternate bytes after an interrupted grab."""

from pathlib import Path
import zipfile

from app.bindery_client import BinderyClient
from app.db import ebook_replacement_for_acquisition, update_ebook_acquisition
from app.staging import verify_staged_ebook


STAGED = "Alternate Fixture.epub"
TITLE = "Alternate Fixture"
AUTHOR = "Fixture Author"


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
        f'<dc:title>{TITLE}</dc:title><dc:creator>{AUTHOR}</dc:creator>'
        '<dc:identifier>alternate-acceptance</dc:identifier></metadata>'
        '<manifest><item id="chapter" href="chapter.xhtml" '
        'media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="chapter"/></spine></package>'
    )
    body = f"{TITLE} by {AUTHOR}. " + ("A fictional passage. " * 100)
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f"<h1>{TITLE}</h1><p>by {AUTHOR}</p><p>{body}</p>"
        "</body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


child = ebook_replacement_for_acquisition(1)
assert child and child["status"] == "grab_requested" and child["admission_id"] is None
path = Path("/staging") / STAGED
write_epub(path)
verification = verify_staged_ebook(int(child["book_id"]), STAGED, BinderyClient())
assert verification["safeToAdmit"] is True, verification["admissionBlockers"]
update_ebook_acquisition(
    int(child["id"]), "verified", queue_id=77,
    staged_relative_path=STAGED, staged_sha256=verification["sha256"],
    verification=verification,
)
print(f"Seeded verified disposable alternate child {child['id']} with independently checked bytes.")
