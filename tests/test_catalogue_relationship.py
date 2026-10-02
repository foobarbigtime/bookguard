"""How a misfiled ebook relates to the catalogued book it really is.

Cases mirror the hand-built checklist from the Unraid library (2026-10-02):
identical copies (Keeper of the Lost Cities -> Nightfall), other editions
(President's Shadow -> The Shadow), an unconfirmed owner file (Sail -> When the
Wind Blows), and books whose only file is an audiobook (Kill -> Kill Alex
Cross), which earlier counted wrongly as duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3

import pytest

from app import verifier
from app.catalogue_relationship import classify_relationship, library_path
from app.config import settings
from app.isbn_evidence import isbn_evidence

PREFIX = "/data/media/books"


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "books"
    root.mkdir()
    monkeypatch.setattr(settings, "ebook_root", str(root))
    monkeypatch.setattr(settings, "ebook_bindery_prefix", PREFIX)
    monkeypatch.setattr(settings, "verification_snapshot_max_bytes", 1024 * 1024)
    return root


def put(root, relative: str, data: bytes) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"{PREFIX}/{relative}"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def owner(book_id: int, title: str, paths: list[str]) -> dict:
    return {"bookId": book_id, "title": title, "ebookFiles": [{"path": p, "size": 0} for p in paths]}


def test_identical_copy_is_a_duplicate_that_loses_nothing(library):
    data = b"Nightfall, the same bytes"
    nightfall = put(library, "Shannon Messenger/Nightfall (2017)/Nightfall.epub", data)

    result = classify_relationship(sha(data), owner(1998, "Nightfall", [nightfall]), lambda _id: "PASS")

    assert result["relationship"] == "duplicate_identical"
    assert "loses nothing" in result["explanation"]


def test_other_edition_of_a_verified_book_is_redundant(library):
    shadow = put(library, "James Patterson/The Shadow (2021)/The Shadow.epub", b"retail edition")

    result = classify_relationship(sha(b"another edition"), owner(7146, "The Shadow", [shadow]), lambda _id: "PASS")

    assert result["relationship"] == "duplicate_edition"
    assert result["ownerScanStatus"] == "PASS"


def test_owner_file_not_yet_confirmed_is_flagged_for_checking(library):
    wind = put(library, "James Patterson/When the wind blows/When the wind blows.epub", b"owner")

    result = classify_relationship(sha(b"mine"), owner(7278, "When the wind blows", [wind]), lambda _id: "REVIEW")

    assert result["relationship"] == "duplicate_unconfirmed"
    assert "Check it before removing" in result["explanation"]


def test_book_without_an_ebook_is_missing_this_file(library):
    result = classify_relationship(sha(b"kill alex cross"), owner(7005, "Kill Alex Cross", []), lambda _id: "PASS")

    assert result["relationship"] == "missing_ebook"
    assert "re-assigned" in result["explanation"]


def test_paths_outside_the_library_or_through_symlinks_are_not_read(library, tmp_path):
    outside = tmp_path / "elsewhere.epub"
    outside.write_bytes(b"x")
    (library / "Author").mkdir()
    (library / "Author" / "link.epub").symlink_to(outside)

    assert library_path("/somewhere/else/book.epub") is None
    assert library_path(f"{PREFIX}/../escape.epub") is None
    assert library_path(f"{PREFIX}/Author/link.epub") is None
    assert library_path(f"{PREFIX}/Author/missing.epub") is None


def test_audiobook_only_book_is_not_counted_as_having_an_ebook(tmp_path, monkeypatch):
    # Regression: an audiobook made "Kill Alex Cross" look like it already had a file.
    db = tmp_path / "bindery.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE books (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT NOT NULL);
            CREATE TABLE editions (id INTEGER PRIMARY KEY, book_id INTEGER, title TEXT,
                                   isbn_13 TEXT, isbn_10 TEXT);
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT,
                                     path TEXT, size_bytes INTEGER NOT NULL DEFAULT 0);
            INSERT INTO authors VALUES (1, 'James Patterson');
            INSERT INTO books VALUES (20, 1, 'Kill'), (21, 1, 'Kill Alex Cross');
            INSERT INTO editions VALUES (1, 21, 'Kill Alex Cross', '9780316198738', NULL);
            INSERT INTO book_files VALUES (1, 21, 'audiobook', '/data/media/audiobooks/Kill Alex Cross', 0);
            """
        )
    monkeypatch.setattr(settings, "bindery_db", str(db))

    found = isbn_evidence(20, ["9780316198738"])["otherOwners"][0]

    assert found["hasFile"] is False
    assert found["ebookFiles"] == []


@dataclass
class _Extracted:
    metadata: dict
    text: str
    identifiers: list
    notes: list
    front_text: str
    source: str


def test_library_verification_records_the_relationship(library, tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(settings, "config_dir", str(config))
    monkeypatch.setattr(settings, "verification_enabled", True)
    verifier.init_verification_db()
    data = b"The Battle of the Labyrinth, retail EPUB bytes"
    mine = library / "Rick Riordan" / "Percy Jackson & the Olympians (2009)" / "copy.txt"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(data)
    real = put(library, "Rick Riordan/The Battle of the Labyrinth (2005)/real.txt", data)
    actual = {"bookId": 1940, "title": "The Battle of the Labyrinth", "author": "Rick Riordan",
              "hasFile": True, "ebookFiles": [{"path": real, "size": len(data)}]}

    monkeypatch.setattr(verifier, "inspect_ebook_security", lambda path, **kw: {
        "safe": True, "checks": {}, "failures": [], "message": "ok"})
    monkeypatch.setattr(verifier, "extract_ebook_identity", lambda path: _Extracted(
        {"title": "The Battle of the Labyrinth", "author": "Rick Riordan"}, "text", [], [], "text", "test"))
    monkeypatch.setattr(verifier, "classify_identity", lambda *a, **k: (
        "WRONG_CONTENT", 99, {"explanation": "ISBN and title identify another book.", "actualBook": actual}))
    monkeypatch.setattr(verifier, "latest_ebook_scan_status", lambda _id: "PASS")

    result = verifier.verify_result({
        "id": 1, "scan_id": "s", "file_id": 2, "book_id": 2174, "format": "ebook",
        "author": "Rick Riordan", "title": "Percy Jackson & the Olympians", "local_path": str(mine),
    })

    assert result["evidence"]["catalogue"]["relationship"] == "duplicate_identical"
    assert result["evidence"]["explanation"].endswith("Removing this copy loses nothing.")
