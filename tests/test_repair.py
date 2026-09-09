from pathlib import Path
import zipfile

from app.config import Settings, settings
from app.db import init_local_db, metadata_repair_by_id
from app.metadata import ebook_metadata
from app.repair import apply_metadata_repair, build_repair_preview, undo_metadata_repair


def _reset_settings():
    defaults = Settings()
    settings.__dict__.update(defaults.__dict__)


def setup_function():
    _reset_settings()


def _make_epub(path: Path, title: str, author: str) -> None:
    container = """<?xml version='1.0'?>
<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container' version='1.0'>
  <rootfiles><rootfile full-path='OEBPS/content.opf' media-type='application/oebps-package+xml'/></rootfiles>
</container>"""
    package = f"""<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf' xmlns:dc='http://purl.org/dc/elements/1.1/' version='3.0'>
  <metadata><dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>
  <manifest/><spine/>
</package>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", package)


def _result(path: Path) -> dict:
    return {
        "id": 101,
        "scan_id": "scan-test",
        "file_id": 202,
        "book_id": 303,
        "author": "Ann Patchett",
        "title": "Bel Canto",
        "format": "ebook",
        "stored_path": "/data/media/books/Ann Patchett/Bel Canto/Bel Canto.epub",
        "local_path": str(path),
        "classification": "PASS",
        "risk_score": 10,
        "reason_code": "SWAPPED_METADATA",
        "reasons": ["Swapped metadata"],
        "metadata": {"title": "Patchett, Ann", "author": "Bel Canto", "source": "epub"},
    }


def test_swapped_epub_preview_does_not_write(tmp_path):
    epub = tmp_path / "book.epub"
    _make_epub(epub, "Patchett, Ann", "Bel Canto")
    settings.metadata_repair_mode = "preview"

    preview = build_repair_preview(_result(epub))
    assert preview["eligible"] is True
    assert preview["safe"] is True
    assert preview["kind"] == "EPUB_METADATA"
    assert preview["before"]["title"] == "Patchett, Ann"
    assert preview["after"]["title"] == "Bel Canto"

    current = ebook_metadata(str(epub))
    assert current["title"] == "Patchett, Ann"
    assert current["author"] == "Bel Canto"


def test_swapped_epub_safe_repair_and_undo(tmp_path):
    epub = tmp_path / "book.epub"
    config = tmp_path / "config"
    config.mkdir()
    _make_epub(epub, "Patchett, Ann", "Bel Canto")

    settings.config_dir = str(config)
    settings.metadata_repair_mode = "safe"
    init_local_db()

    applied = apply_metadata_repair(_result(epub))
    repair_id = applied["repair_id"]
    current = ebook_metadata(str(epub))
    assert current["title"] == "Bel Canto"
    assert current["author"] == "Ann Patchett"
    assert metadata_repair_by_id(repair_id)["status"] == "applied"

    undo_metadata_repair(repair_id)
    restored = ebook_metadata(str(epub))
    assert restored["title"] == "Patchett, Ann"
    assert restored["author"] == "Bel Canto"
    assert metadata_repair_by_id(repair_id)["status"] == "undone"
