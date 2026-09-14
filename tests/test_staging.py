from __future__ import annotations

import zipfile

import pytest

import app.ebook_extraction as ebook_extraction
import app.staging as staging


class FakeClient:
    def __init__(self, title: str, author: str):
        self.title = title
        self.author = author

    def get_book(self, book_id: int):
        return {
            "id": book_id,
            "title": self.title,
            "author": {"name": self.author},
        }


def _write_epub(path, title: str, author: str, body: str) -> None:
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    package = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:identifier>test-id</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter"/>
  </spine>
</package>
"""
    chapter = f"""<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>{title}</h1><p>by {author}</p><p>{body}</p>
</body></html>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


def _configure(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    root.mkdir()
    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(root))
    monkeypatch.setattr(ebook_extraction.settings, "verification_use_tika", False)
    return root


def test_verified_correct_staged_epub_is_eligible_but_not_admitted(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    path = root / "Bel Canto - Ann Patchett.epub"
    _write_epub(
        path,
        "Bel Canto",
        "Ann Patchett",
        "Bel Canto by Ann Patchett. " + ("A fictional passage. " * 80),
    )

    result = staging.verify_staged_ebook(
        42,
        path.name,
        FakeClient("Bel Canto", "Ann Patchett"),
    )

    assert result["verdict"] == "VERIFIED_CORRECT"
    assert result["confidence"] == 99
    assert result["stableDuringVerification"] is True
    assert result["safeToAdmit"] is True
    assert result["readOnly"] is True
    assert result["sha256"]
    assert "separate readiness checks" in result["message"]
    assert result["evidence"]["security"]["safe"] is True


def test_corrupt_staged_epub_is_blocked_before_identity_verification(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    path = root / "Bel Canto - Ann Patchett.epub"
    path.write_bytes(b"not an epub")

    result = staging.verify_staged_ebook(
        42,
        path.name,
        FakeClient("Bel Canto", "Ann Patchett"),
    )

    assert result["verdict"] == "UNSAFE_FILE"
    assert result["source"] == "deterministic-safety"
    assert result["safeToAdmit"] is False
    assert "deterministicSecurityChecksFailed" in result["admissionBlockers"]
    assert result["evidence"]["security"]["safe"] is False


def test_wrong_staged_epub_is_never_eligible(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    path = root / "Bel Canto - Ann Patchett.epub"
    _write_epub(
        path,
        "Death of a Texan",
        "Cat Hickey",
        "Death of a Texan by Cat Hickey. " + ("Different book content. " * 80),
    )

    result = staging.verify_staged_ebook(
        42,
        path.name,
        FakeClient("Bel Canto", "Ann Patchett"),
    )

    assert result["verdict"] == "WRONG_CONTENT"
    assert result["confidence"] == 99
    assert result["safeToAdmit"] is False
    assert "verdict:WRONG_CONTENT" in result["admissionBlockers"]


def test_staged_path_traversal_is_rejected(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"not an epub")

    with pytest.raises(staging.StagingSafetyError, match="outside"):
        staging.verify_staged_ebook(
            42,
            "../outside.epub",
            FakeClient("Bel Canto", "Ann Patchett"),
        )


def test_staged_symlink_escape_is_rejected(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"not an epub")
    (root / "escape.epub").symlink_to(outside)

    with pytest.raises(staging.StagingSafetyError, match="Symlinked"):
        staging.verify_staged_ebook(
            42,
            "escape.epub",
            FakeClient("Bel Canto", "Ann Patchett"),
        )


def test_staging_inventory_lists_only_supported_regular_files(tmp_path, monkeypatch):
    root = _configure(tmp_path, monkeypatch)
    _write_epub(
        root / "book.epub",
        "Bel Canto",
        "Ann Patchett",
        "Bel Canto by Ann Patchett. " + ("A fictional passage. " * 80),
    )
    (root / "notes.docx").write_bytes(b"x")
    (root / ".hidden.epub").write_bytes(b"x")

    result = staging.list_staged_ebooks()

    assert result["count"] == 1
    assert result["items"][0]["relativePath"] == "book.epub"
    assert result["readOnly"] is True
