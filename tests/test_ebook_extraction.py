import zipfile

import app.ebook_extraction as ebook_extraction
from app.archive_io import read_zip_member_prefix
from app.config import settings
import app.pdf_probe as pdf_probe
from pypdf import PdfWriter


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



def test_pdf_content_extraction_runs_in_isolated_worker(tmp_path):
    path = tmp_path / "book.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "Bel Canto", "/Author": "Ann Patchett"})
    with path.open("wb") as handle:
        writer.write(handle)

    payload = pdf_probe.extract_pdf_text(
        path,
        max_chars=5000,
        page_limit=3,
        front_chars=1000,
    )

    assert "error" not in payload
    assert payload["title"] == "Bel Canto"
    assert payload["author"] == "Ann Patchett"
    assert payload["text"] == ""
    assert payload["front_text"] == ""


def test_pdf_identity_routes_through_isolated_probe(monkeypatch):
    observed = {}

    def fake_extract(path, *, max_chars, page_limit, front_chars):
        observed.update(
            {
                "path": path,
                "max_chars": max_chars,
                "page_limit": page_limit,
                "front_chars": front_chars,
            }
        )
        return {
            "title": "Bel Canto",
            "author": "Ann Patchett",
            "text": "Bel Canto by Ann Patchett",
            "front_text": "Bel Canto by Ann Patchett",
        }

    monkeypatch.setattr(pdf_probe, "extract_pdf_text", fake_extract)
    monkeypatch.setattr(settings, "verification_max_text_chars", 1234)
    monkeypatch.setattr(settings, "verification_pdf_pages", 7)

    metadata, text, identifiers, front = ebook_extraction.extract_pdf_identity(
        "/books/Bel Canto.pdf"
    )

    assert observed == {
        "path": "/books/Bel Canto.pdf",
        "max_chars": 1234,
        "page_limit": 7,
        "front_chars": 160000,
    }
    assert metadata == {
        "title": "Bel Canto",
        "author": "Ann Patchett",
        "source": "pdf",
    }
    assert text == "Bel Canto by Ann Patchett"
    assert front == "Bel Canto by Ann Patchett"
    assert identifiers == []


def test_pdf_content_extraction_rejects_unbounded_limits_before_worker(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "book.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("PDF worker must not run with an unsafe text limit.")

    monkeypatch.setattr(pdf_probe.subprocess, "run", unexpected_run)

    payload = pdf_probe.extract_pdf_text(
        path,
        max_chars=pdf_probe.MAX_CONTENT_TEXT_CHARS + 1,
        page_limit=1,
        front_chars=1000,
    )

    assert "outside the allowed range" in payload["error"]



def test_plain_identity_reads_only_configured_prefix(tmp_path, monkeypatch):
    path = tmp_path / "book.txt"
    path.write_bytes(
        b"Title: Bel Canto\nAuthor: Ann Patchett\n"
        + (b"story " * 200000)
    )
    monkeypatch.setattr(settings, "verification_max_text_chars", 1000)

    observed = {}
    real_reader = ebook_extraction.read_file_prefix

    def tracked_reader(target, *, max_bytes):
        observed["max_bytes"] = max_bytes
        return real_reader(target, max_bytes=max_bytes)

    monkeypatch.setattr(
        ebook_extraction,
        "read_file_prefix",
        tracked_reader,
    )

    metadata, text, identifiers, front = ebook_extraction.extract_plain_identity(
        str(path)
    )

    assert observed["max_bytes"] == 2000
    assert metadata["title"] == "Bel Canto"
    assert metadata["author"] == "Ann Patchett"
    assert identifiers == []
    assert len(text) <= 1000
    assert len(front) <= 1000
