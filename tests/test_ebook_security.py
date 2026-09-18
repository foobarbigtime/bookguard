from __future__ import annotations

import zipfile
import stat

from pypdf import PdfWriter

import app.archive_io as archive_io
import app.ebook_security as ebook_security
import app.pdf_probe as pdf_probe
from app.ebook_security import inspect_ebook_security


def _write_epub(
    path,
    *,
    package_path: str = "OEBPS/content.opf",
    include_package: bool = True,
    conforming_mimetype: bool = True,
    mimetype_value: str = "application/epub+zip",
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
        if conforming_mimetype:
            archive.writestr(
                zipfile.ZipInfo("mimetype"),
                mimetype_value,
                compress_type=zipfile.ZIP_STORED,
            )
        archive.writestr("META-INF/container.xml", container)
        if not conforming_mimetype:
            archive.writestr(
                "mimetype",
                mimetype_value,
                compress_type=zipfile.ZIP_DEFLATED,
            )
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
    assert report["checks"]["archiveSafety"]["status"] == "passed"
    assert report["checks"]["epubStructure"]["status"] == "passed"
    assert report["checks"]["pdfIntegrity"]["status"] == "not_applicable"


def test_nonconforming_mimetype_packaging_is_a_non_blocking_warning(tmp_path):
    path = tmp_path / "legacy.epub"
    _write_epub(
        path,
        conforming_mimetype=False,
        mimetype_value="application/epub+zip\r\n",
    )

    report = inspect_ebook_security(path)

    structure = report["checks"]["epubStructure"]
    assert report["safe"] is True
    assert report["failures"] == []
    assert structure["status"] == "warning"
    assert structure["warnings"] == [
        "The mimetype file contains a UTF-8 BOM or surrounding ASCII whitespace.",
        "The mimetype file is not the first archive member.",
        "The mimetype file is compressed instead of stored.",
    ]


def test_missing_mimetype_remains_a_blocking_failure(tmp_path):
    path = tmp_path / "missing-mimetype.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", "<container/>")

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert report["checks"]["epubStructure"]["status"] == "failed"
    assert "mimetype file is absent" in report["checks"]["epubStructure"]["message"]


def test_incorrect_mimetype_remains_a_blocking_failure(tmp_path):
    path = tmp_path / "wrong-mimetype.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/zip")
        archive.writestr("META-INF/container.xml", "<container/>")

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert report["checks"]["epubStructure"]["status"] == "failed"
    assert "not application/epub+zip" in report["checks"]["epubStructure"]["message"]


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


def test_epub_structure_bounds_mimetype_read(tmp_path, monkeypatch):
    monkeypatch.setattr(ebook_security, "EPUB_MIMETYPE_MAX_BYTES", 24)
    path = tmp_path / "oversized-mimetype.epub"
    _write_epub(path, mimetype_value="application/epub+zip     ")

    report = inspect_ebook_security(path)

    structure = report["checks"]["epubStructure"]
    assert report["safe"] is False
    assert structure["status"] == "failed"
    assert "mimetype" in structure["message"]
    assert "24-byte metadata limit" in structure["message"]


def test_epub_structure_bounds_container_xml_read(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_io, "DEFAULT_XML_MEMBER_LIMIT", 128)
    path = tmp_path / "oversized-container.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", b"x" * 129)

    report = inspect_ebook_security(path)

    structure = report["checks"]["epubStructure"]
    assert report["safe"] is False
    assert structure["status"] == "failed"
    assert "META-INF/container.xml" in structure["message"]
    assert "128-byte metadata limit" in structure["message"]


def test_epub_structure_bounds_package_xml_read(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_io, "DEFAULT_XML_MEMBER_LIMIT", 512)
    path = tmp_path / "oversized-package.epub"
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", b"x" * 513)

    report = inspect_ebook_security(path)

    structure = report["checks"]["epubStructure"]
    assert report["safe"] is False
    assert structure["status"] == "failed"
    assert "OEBPS/content.opf" in structure["message"]
    assert "512-byte metadata limit" in structure["message"]


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


def test_archive_path_traversal_fails_closed(tmp_path):
    path = tmp_path / "traversal.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../outside.txt", "unsafe")

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert report["checks"]["archiveSafety"]["status"] == "failed"
    assert "unsafe member path" in report["checks"]["archiveSafety"]["message"]


def test_archive_case_collision_fails_closed(tmp_path):
    path = tmp_path / "collision.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Images/Page01.jpg", "one")
        archive.writestr("images/page01.JPG", "two")

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert "case-colliding" in report["checks"]["archiveSafety"]["message"]


def test_archive_symlink_member_fails_closed(tmp_path):
    path = tmp_path / "link.cbz"
    link = zipfile.ZipInfo("page-link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(link, "target")

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert "symbolic-link" in report["checks"]["archiveSafety"]["message"]


def test_archive_encrypted_member_fails_closed(tmp_path):
    path = tmp_path / "encrypted.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("page.txt", "content")

    raw = bytearray(path.read_bytes())
    local_header = raw.index(b"PK\x03\x04")
    central_header = raw.index(b"PK\x01\x02")
    raw[local_header + 6] |= 0x01
    raw[central_header + 8] |= 0x01
    path.write_bytes(raw)

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert "encrypted member" in report["checks"]["archiveSafety"]["message"]


def test_archive_member_limit_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(ebook_security, "MAX_ARCHIVE_MEMBERS", 1)
    path = tmp_path / "many.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one.txt", "one")
        archive.writestr("two.txt", "two")

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert "members; the limit" in report["checks"]["archiveSafety"]["message"]


def test_dangerous_member_expansion_ratio_fails_before_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(ebook_security, "MIN_RATIO_CHECK_BYTES", 1)
    monkeypatch.setattr(ebook_security, "MAX_ARCHIVE_MEMBER_RATIO", 2)
    path = tmp_path / "expansion.cbz"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("page.txt", b"0" * 4096)

    report = inspect_ebook_security(path)

    assert report["safe"] is False
    assert "expansion ratio" in report["checks"]["archiveSafety"]["message"]


def test_checks_can_be_explicitly_disabled(tmp_path):
    path = tmp_path / "broken.epub"
    path.write_bytes(b"not an epub")

    report = inspect_ebook_security(
        path,
        check_file_signatures=False,
        check_archive_safety=False,
        check_epub_structure=False,
    )

    assert report["safe"] is True
    assert report["checks"]["fileSignature"]["status"] == "disabled"
    assert report["checks"]["archiveSafety"]["status"] == "disabled"
    assert report["checks"]["epubStructure"]["status"] == "disabled"



def test_pdf_integrity_routes_through_isolated_probe(tmp_path, monkeypatch):
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.7\nbody\n%%EOF\n")

    observed = {}

    def fake_integrity(target):
        observed["path"] = target
        return {"pages": 3, "encrypted": False}

    monkeypatch.setattr(pdf_probe, "inspect_pdf_integrity", fake_integrity)

    report = inspect_ebook_security(
        path,
        check_file_signatures=False,
        check_archive_safety=False,
        check_epub_structure=False,
    )

    assert observed["path"] == path
    assert report["safe"] is True
    assert report["checks"]["pdfIntegrity"]["status"] == "passed"
    assert report["checks"]["pdfIntegrity"]["pages"] == 3


def test_pdf_integrity_worker_failure_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.7\nbody\n%%EOF\n")

    monkeypatch.setattr(
        pdf_probe,
        "inspect_pdf_integrity",
        lambda target: {"error": "isolated parser failed"},
    )

    report = inspect_ebook_security(
        path,
        check_file_signatures=False,
        check_archive_safety=False,
        check_epub_structure=False,
    )

    assert report["safe"] is False
    assert report["checks"]["pdfIntegrity"]["status"] == "failed"
    assert "isolated parser failed" in report["checks"]["pdfIntegrity"]["message"]


def test_pdf_integrity_probe_runs_in_worker(tmp_path):
    path = tmp_path / "book.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)

    payload = pdf_probe.inspect_pdf_integrity(path)

    assert "error" not in payload
    assert payload["pages"] == 1
    assert payload["encrypted"] is False
