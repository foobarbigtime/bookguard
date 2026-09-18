import zipfile

import app.ebook_extraction as ebook_extraction
from app.archive_io import read_zip_member_prefix
from app.config import settings


def _build_epub(path, chapter: bytes) -> None:
    container = """<?xml version='1.0'?>
<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>
  <rootfiles>
    <rootfile full-path='OEBPS/content.opf'
              media-type='application/oebps-package+xml'/>
  </rootfiles>
</container>"""
    package = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf'
         xmlns:dc='http://purl.org/dc/elements/1.1/' version='3.0'>
  <metadata>
    <dc:title>Bel Canto</dc:title>
    <dc:creator>Ann Patchett</dc:creator>
  </metadata>
  <manifest>
    <item id='chapter' href='chapter.xhtml'
          media-type='application/xhtml+xml'/>
  </manifest>
  <spine>
    <itemref idref='chapter'/>
  </spine>
</package>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


def test_zip_member_prefix_stops_before_full_member_expansion(tmp_path):
    path = tmp_path / "large.zip"
    payload = b"A" * (1024 * 1024)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("chapter.xhtml", payload)

    with zipfile.ZipFile(path) as archive:
        prefix = read_zip_member_prefix(
            archive,
            "chapter.xhtml",
            max_bytes=4096,
        )

    assert prefix == payload[:4096]
    assert len(prefix) == 4096


def test_epub_identity_caps_total_raw_html_expansion(tmp_path, monkeypatch):
    path = tmp_path / "book.epub"
    chapter = (
        b"<html><body>Bel Canto by Ann Patchett "
        + (b"story " * 200000)
        + b"</body></html>"
    )
    _build_epub(path, chapter)
    monkeypatch.setattr(settings, "verification_max_text_chars", 100)

    observed_limits = []
    real_reader = ebook_extraction.read_zip_member_prefix

    def tracked_reader(archive, name, *, max_bytes):
        observed_limits.append(max_bytes)
        return real_reader(archive, name, max_bytes=max_bytes)

    monkeypatch.setattr(
        ebook_extraction,
        "read_zip_member_prefix",
        tracked_reader,
    )

    metadata, text, identifiers, front = ebook_extraction.extract_epub_identity(
        str(path)
    )

    assert metadata["title"] == "Bel Canto"
    assert metadata["author"] == "Ann Patchett"
    assert identifiers == []
    assert observed_limits == [400]
    assert len(text) <= 100
    assert len(front) <= 400
