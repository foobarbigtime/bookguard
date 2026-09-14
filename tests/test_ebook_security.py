from __future__ import annotations

import zipfile

from pypdf import PdfWriter

from app.ebook_security import inspect_ebook_security


def _write_epub(
    path,
    *,
    package_path: str = "OEBPS/content.opf",
    include_package: bool = True,
) -> None:
    container = f"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{package_path}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    package = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata/>
  <manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        if include_package:
            archive.writestr(package_path, package)
        archive.writestr("OEBPS/chapter.xhtml", "<html><body>Test</body></html>")


def test_valid_epub_passes_signature_and_structure(tmp_path):
    path = tmp_path / "book.epub"
    _write_epub(path)

    report = inspect_ebook_security(path)

    assert report["safe"] is True
    assert report["readOnly"] is True
    assert report["failures"] == []
    assert report["checks"]["fileSignature"]["status"] == "passed"
    assert report["checks"]["epubStructure"]["status"] == "passed"
    assert report["checks"]["pdfIntegrity"]["status"] == "not_applicable"


def test_pdf_renamed_as_epub_fails_closed(tmp_path):
    path = tmp_path / "book.epub"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert "fileSignature" in report["failures"]
    assert report["checks"]["fileSignature"]["detectedFormat"] == "pdf"


def test_epub_with_missing_package_fails_structure(tmp_path):
    path = tmp_path / "book.epub"
    _write_epub(path, package_path="OEBPS/missing.opf", include_package=False)

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert "epubStructure" in report["failures"]


def test_valid_pdf_passes_signature_and_integrity(tmp_path):
    path = tmp_path / "book.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)

    report = inspect_ebook_security(path)

    assert report["safe"] is True
    assert report["checks"]["fileSignature"]["status"] == "passed"
    assert report["checks"]["pdfIntegrity"]["status"] == "passed"
    assert report["checks"]["pdfIntegrity"]["pages"] == 1


def test_truncated_pdf_fails_integrity(tmp_path):
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.7\nthis is incomplete")

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert report["checks"]["fileSignature"]["status"] == "passed"
    assert report["checks"]["pdfIntegrity"]["status"] == "failed"


def test_plain_text_signature_is_explicitly_not_applicable(tmp_path):
    path = tmp_path / "book.txt"
    path.write_text("A book", encoding="utf-8")

    report = inspect_ebook_security(path)

    assert report["safe"] is True
    assert report["checks"]["fileSignature"]["status"] == "not_applicable"


def test_checks_can_be_explicitly_disabled(tmp_path):
    path = tmp_path / "broken.epub"
    path.write_bytes(b"not an epub")

    report = inspect_ebook_security(
        path,
        check_file_signatures=False,
        check_epub_structure=False,
    )

    assert report["safe"] is True
    assert report["checks"]["fileSignature"]["status"] == "disabled"
    assert report["checks"]["epubStructure"]["status"] == "disabled"
