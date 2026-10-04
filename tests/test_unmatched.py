"""Bindery's unmatched files: junk, not in the library, or which book (with evidence)."""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from app import unmatched
from app.config import settings
from app.db import init_local_db

WORDS = " ".join(["the quiet house stood by the river and she walked home with her dog"] * 60)


def make_epub(path: Path, *, title: str = "", author: str = "", body: str = WORDS, extra: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = (f"<dc:title>{title}</dc:title>" if title else "") + (f"<dc:creator>{author}</dc:creator>" if author else "")
    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        epub.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"))
        epub.writestr("OEBPS/content.opf", (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">x</dc:identifier>{meta}'
            "<dc:language>en</dc:language></metadata>"
            '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>'))
        epub.writestr("OEBPS/c1.xhtml", (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
            f"<h1>{title}</h1><p>{author}</p><p>{body}</p></body></html>"))
        for name, data in (extra or {}).items():
            epub.writestr(name, data)
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "ebook_root", str(tmp_path / "books"))
    monkeypatch.setattr(settings, "ebook_bindery_prefix", "/data/books")
    monkeypatch.setattr(settings, "audiobook_root", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(settings, "audiobook_bindery_prefix", "/data/audiobooks")
    monkeypatch.setattr(settings, "verification_malware_scan", False)
    monkeypatch.setattr(unmatched, "bindery_files_for_book", lambda book_id, fmt: [])
    init_local_db()
    library = unmatched.Library([
        {"id": 7236, "title": "Lights Out", "author": "James Patterson"},
        {"id": 12, "title": "Katt vs. Dogg", "author": "James Patterson"},
    ])
    return {"root": tmp_path / "books", "audio": tmp_path / "audiobooks", "library": library}


def row(rel: str, fmt: str = "ebook", **extra) -> dict:
    parts = rel.split("/")
    return {"id": 1, "kind": "file", "format": fmt, "rootPath": f"/data/{'audiobooks' if fmt == 'audiobook' else 'books'}",
            "relPath": rel, "authorFolder": parts[0] if len(parts) > 1 else "", "members": [parts[-1]], **extra}


def test_a_tiny_fake_ebook_is_junk(lib):
    (lib["root"] / "James Patterson/Die 6 Geisel").mkdir(parents=True)
    (lib["root"] / "James Patterson/Die 6 Geisel/Die 6 Geisel.epub").write_bytes(b"x" * 1008)

    outcome = unmatched.check_item(row("James Patterson/Die 6 Geisel/Die 6 Geisel.epub"), lib["library"], None)

    assert outcome["verdict"] == "JUNK"
    assert "1008 bytes" in outcome["reason"]


def test_a_real_ebook_proven_by_two_sources_belongs_to_its_book(lib):
    make_epub(lib["root"] / "James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub",
              title="Lights Out", author="James Patterson")

    outcome = unmatched.check_item(
        row("James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub"), lib["library"], None)

    assert (outcome["verdict"], outcome["bookId"]) == ("BELONGS", 7236)
    assert "Embedded metadata" in outcome["reason"] and "Title page" in outcome["reason"]


def test_a_real_book_by_an_author_not_in_the_library(lib):
    make_epub(lib["root"] / "Misc/Tycoon/Tycoon - Katy Evans.epub", title="Tycoon", author="Katy Evans")

    outcome = unmatched.check_item(row("Misc/Tycoon/Tycoon - Katy Evans.epub"), lib["library"], None)

    assert outcome["verdict"] == "NOT_IN_LIBRARY"
    assert "“Tycoon” by Katy Evans" in outcome["reason"]


def test_file_and_folder_names_alone_never_prove_a_book(lib):
    # No embedded title; the names and even the first page say "Lights Out" / "James Patterson".
    make_epub(lib["root"] / "James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub",
              body="Lights Out James Patterson " + WORDS)

    outcome = unmatched.check_item(
        row("James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub"), lib["library"], None)

    assert outcome["verdict"] == "UNSURE"


def test_a_sample_is_junk(lib):
    make_epub(lib["root"] / "A/B/Lights Out - James Patterson.epub", title="Lights Out", author="James Patterson",
              body=WORDS + " This is a free sample. Buy the full book to keep reading.")

    outcome = unmatched.check_item(row("A/B/Lights Out - James Patterson.epub"), lib["library"], None)

    assert outcome["verdict"] == "JUNK" and "sample" in outcome["reason"]


def test_a_drm_locked_ebook_is_junk(lib):
    make_epub(lib["root"] / "A/B/Locked.epub", title="Lights Out", author="James Patterson", extra={
        "META-INF/encryption.xml": '<encryption><EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/></encryption>'})

    outcome = unmatched.check_item(row("A/B/Locked.epub"), lib["library"], None)

    assert outcome["verdict"] == "JUNK"


def _audio_row(lib, monkeypatch, *, silent=False, tags=None):
    folder = lib["audio"] / "James Patterson/$10,000,000 Marriage Proposal"
    folder.mkdir(parents=True)
    names = [f"Katy Evans - Tycoon {n}-7.mp3" for n in range(1, 8)]
    for name in names:
        (folder / name).write_bytes(b"ID3")
    probe = {"audio_stream_count": 1, "duration_seconds": 1800, "album": "Tycoon", "artist": "Katy Evans", **(tags or {})}
    monkeypatch.setattr(unmatched, "cached_or_probe_audio_file", lambda path: ({"path": path, **probe}, False))
    monkeypatch.setattr(unmatched, "disc_track_sequence_warnings", lambda probes: [])
    monkeypatch.setattr(unmatched, "_silent", lambda path: silent)
    return {"id": 2, "kind": "folder", "format": "audiobook", "rootPath": "/data/audiobooks",
            "relPath": "James Patterson/$10,000,000 Marriage Proposal", "authorFolder": "James Patterson",
            "members": names}


def test_the_katy_evans_audiobook_in_a_patterson_folder_is_not_in_the_library(lib, monkeypatch):
    outcome = unmatched.check_item(_audio_row(lib, monkeypatch), lib["library"], None)

    assert outcome["verdict"] == "NOT_IN_LIBRARY"
    assert "“Tycoon” by Katy Evans" in outcome["reason"]
    folder = next(e for e in outcome["evidence"] if e["check"] == "Folder")
    assert folder["result"] == "warn" and "James Patterson" in folder["detail"]


def test_silent_audio_is_junk(lib, monkeypatch):
    outcome = unmatched.check_item(_audio_row(lib, monkeypatch, silent=True), lib["library"], None)

    assert outcome["verdict"] == "JUNK" and "silent" in outcome["reason"]


class FakeClient:
    def __init__(self, items):
        self.items, self.adopted = items, []

    def list_unmatched(self, *, search="", file_format=None, offset=0):
        page = self.items[offset:offset + 1]  # one per page proves paging
        return {"items": page, "total": len(self.items)}

    def lookup_isbn(self, isbn):
        return {}

    def adopt_unmatched(self, row_id, book_id):
        self.adopted.append((row_id, book_id))


def test_check_all_then_attach_only_a_proven_file(lib, monkeypatch):
    make_epub(lib["root"] / "James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub",
              title="Lights Out", author="James Patterson")
    (lib["root"] / "Junk").mkdir()
    (lib["root"] / "Junk/x.epub").write_bytes(b"x" * 10)
    client = FakeClient([
        {**row("James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub"), "id": 11},
        {**row("Junk/x.epub"), "id": 12},
    ])
    monkeypatch.setattr(unmatched, "library_books", lambda: list(lib["library"].books.values()))
    monkeypatch.setattr(unmatched, "BinderyClient", lambda **kwargs: client)
    monkeypatch.setattr(unmatched, "resolve_bindery_api_key", lambda: "key")
    monkeypatch.setattr(settings, "allow_actions", True)

    unmatched.check_unmatched(client)
    verdicts = {i["row_id"]: i["verdict"] for i in unmatched.stored_checks()}
    assert verdicts == {11: "BELONGS", 12: "JUNK"}

    with pytest.raises(unmatched.ActionError, match="Only files BookGuard proved"):
        unmatched.attach(12)
    assert "Lights Out" in unmatched.attach(11)
    assert client.adopted == [(11, 7236)]
    assert {i["row_id"] for i in unmatched.stored_checks()} == {12}


def test_unmatched_page_shows_each_verdict_with_its_reason(lib, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routes.pages import router

    unmatched.init_unmatched_db()
    item = {**row("A/B/x.epub"), "parsedTitle": "Die 6 Geisel"}
    unmatched._save(5, "sig", item, {"verdict": "JUNK", "reason": "The file is only 1008 bytes.",
                                     "evidence": [{"check": "Length", "result": "fail", "detail": "tiny"}]})
    unmatched._save(6, "sig", {**item, "parsedTitle": "Lights Out"},
                    {"verdict": "BELONGS", "reason": "It is “Lights Out”.", "bookId": 7236, "bookTitle": "Lights Out",
                     "evidence": []})
    monkeypatch.setattr(settings, "allow_actions", True)
    app = FastAPI()
    app.include_router(router)
    page = TestClient(app).get("/review/unmatched").text

    assert "Junk" in page and "The file is only 1008 bytes." in page
    assert 'data-unmatched-attach="6"' in page and "Attach to “Lights Out”" in page
