#!/usr/bin/env python3
"""Create one safe disposable replacement EPUB in external staging."""

from pathlib import Path
import zipfile


STAGED = Path("/staging/Conflict Fixture.epub")
TITLE = "Conflict Fixture"
AUTHOR = "Conflict Author"

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
    '<dc:identifier>quarantine-handoff-acceptance</dc:identifier></metadata>'
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
with zipfile.ZipFile(STAGED, "w") as archive:
    archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                     compress_type=zipfile.ZIP_STORED)
    archive.writestr("META-INF/container.xml", container)
    archive.writestr("OEBPS/content.opf", package)
    archive.writestr("OEBPS/chapter.xhtml", chapter)
print("Seeded one safe disposable replacement in external staging.")
