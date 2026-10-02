"""ISBN evidence: checksum-valid ISBNs, Bindery edition ownership, and the rule
that an ISBN only decides something when the file's own title agrees.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.config import settings
from app.isbn_evidence import file_isbns, isbn13, isbn_evidence
from app.verification_engine import classify_identity

OLD_MARS = "9780345537270"
KILL_ALEX_CROSS = "9780316198738"
ELEVEN_22_63 = "9781451627282"


def _front(title: str, author: str) -> str:
    return f"{title}\n{author}\n\nCopyright notice. " + ("Chapter one begins here. " * 60)


@pytest.fixture
def bindery(tmp_path, monkeypatch):
    path = tmp_path / "bindery.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE books (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT NOT NULL);
            CREATE TABLE editions (id INTEGER PRIMARY KEY, book_id INTEGER, title TEXT,
                                   isbn_13 TEXT, isbn_10 TEXT);
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT,
                                     path TEXT, size_bytes INTEGER NOT NULL DEFAULT 0);
            INSERT INTO authors VALUES (1, 'James S. A. Corey'), (2, 'James Patterson'), (3, 'Stephen King');
            INSERT INTO books VALUES (10, 1, 'Old Mars'), (20, 2, 'Kill'), (21, 2, 'Kill Alex Cross'),
                                     (30, 3, 'Der Anschlag'), (31, 3, '11/22/63');
            INSERT INTO editions VALUES (1, 10, 'Old Mars', '978-0-345-53727-0', NULL),
                                        (2, 21, 'Kill Alex Cross', NULL, '0316198730'),
                                        (3, 31, '11/22/63', '9781451627282', NULL);
            INSERT INTO book_files VALUES (1, 21, 'ebook', '/books/Kill Alex Cross.epub', 0);
            """
        )
    monkeypatch.setattr(settings, "bindery_db", str(path))
    return path


# --- normalisation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("978-0-306-40615-7", "9780306406157"),
        ("0-306-40615-2", "9780306406157"),
        ("080442957X", "9780804429573"),
        ("9780306406158", None),  # bad checksum
        ("1234567890", None),
        ("urn:uuid:5b6f2f1e", None),
    ],
)
def test_isbn13_accepts_only_checksum_valid_isbns(raw, expected):
    assert isbn13(raw) == expected


def test_file_isbns_ignores_asins_uuids_and_duplicates():
    identifiers = ["urn:isbn:9780306406157", "asin:B0756W44M5", "urn:uuid:1234",
                   "isbn:0306406152", "calibre:42"]
    assert file_isbns(identifiers) == ["9780306406157"]


# --- Bindery ownership -------------------------------------------------------


def test_isbn_matching_the_expected_books_edition(bindery):
    evidence = isbn_evidence(10, [f"urn:isbn:{OLD_MARS}"])
    assert evidence["expectedMatch"] is True
    assert evidence["otherOwners"] == []


def test_isbn_ten_owned_by_another_book_is_reported_with_its_file_status(bindery):
    evidence = isbn_evidence(20, [KILL_ALEX_CROSS])
    assert evidence["expectedMatch"] is False
    assert evidence["otherOwners"] == [
        {
            "bookId": 21, "title": "Kill Alex Cross", "author": "James Patterson", "hasFile": True,
            "ebookFiles": [{"path": "/books/Kill Alex Cross.epub", "size": 0}],
        },
    ]


def test_unavailable_bindery_yields_only_the_isbns(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bindery_db", str(tmp_path / "missing.db"))
    assert isbn_evidence(10, [OLD_MARS]) == {
        "isbns": [OLD_MARS], "expectedMatch": False, "otherOwners": [],
    }


# --- classifier rules ------------------------------------------------------


def test_anthology_verifies_when_isbn_and_title_agree(bindery):
    # Catalogued under one contributor; the title page names the editors.
    result = {"title": "Old Mars", "author": "James S. A. Corey"}
    metadata = {"title": "Old Mars", "author": "George R. R. Martin; Gardner Dozois"}
    text = _front("OLD MARS", "EDITED BY GEORGE R. R. MARTIN AND GARDNER DOZOIS")

    without = classify_identity(result, metadata, text, [OLD_MARS], [], text)
    with_isbn = classify_identity(
        {**result, "isbn_evidence": isbn_evidence(10, [OLD_MARS])}, metadata, text, [OLD_MARS], [], text,
    )

    assert without[0] == "INSUFFICIENT_EVIDENCE"
    assert with_isbn[:2] == ("VERIFIED_CORRECT", 99)
    assert "ISBN is one of this book's editions" in with_isbn[2]["explanation"]


def test_file_that_is_another_catalogued_book_is_identified(bindery):
    result = {"title": "Kill", "author": "James Patterson"}
    metadata = {"title": "Kill Alex Cross", "author": "James Patterson"}
    text = _front("KILL ALEX CROSS", "JAMES PATTERSON")

    verdict, confidence, evidence = classify_identity(
        {**result, "isbn_evidence": isbn_evidence(20, [KILL_ALEX_CROSS])},
        metadata, text, [KILL_ALEX_CROSS], [], text,
    )

    assert (verdict, confidence) == ("WRONG_CONTENT", 99)
    assert evidence["actualBook"]["bookId"] == 21
    assert "Kill Alex Cross" in evidence["explanation"]


def test_a_translation_whose_title_matches_the_expected_book_is_not_called_wrong(bindery):
    # The German edition of 11/22/63 carries an ISBN Bindery files under 11/22/63.
    result = {"title": "Der Anschlag", "author": "Stephen King"}
    metadata = {"title": "Der Anschlag", "author": "King, Stephen"}
    text = "Die Geschichte beginnt. " * 80

    verdict, _confidence, evidence = classify_identity(
        {**result, "isbn_evidence": isbn_evidence(30, [ELEVEN_22_63])},
        metadata, text, [ELEVEN_22_63], [], text,
    )

    assert verdict != "WRONG_CONTENT"
    assert "actualBook" not in evidence


def test_an_isbn_alone_never_decides(bindery):
    # The ISBN points at Kill Alex Cross, but the file's own title says otherwise.
    result = {"title": "Kill", "author": "James Patterson"}
    metadata = {"title": "Untitled Manuscript", "author": ""}
    text = "Some unrelated text. " * 80

    verdict, _confidence, _evidence = classify_identity(
        {**result, "isbn_evidence": isbn_evidence(20, [KILL_ALEX_CROSS])},
        metadata, text, [KILL_ALEX_CROSS], [], text,
    )

    assert verdict == "INSUFFICIENT_EVIDENCE"


def test_an_isbn_of_the_expected_book_sends_wrong_content_back_for_review(bindery):
    result = {"title": "Old Mars", "author": "James S. A. Corey"}
    metadata = {"title": "Dead After Dark", "author": "Sherrilyn Kenyon"}
    text = _front("DEAD AFTER DARK", "SHERRILYN KENYON")

    plain = classify_identity(result, metadata, text, [OLD_MARS], [], text)
    with_isbn = classify_identity(
        {**result, "isbn_evidence": isbn_evidence(10, [OLD_MARS])}, metadata, text, [OLD_MARS], [], text,
    )

    assert plain[0] == "WRONG_CONTENT"
    assert with_isbn[:2] == ("INSUFFICIENT_EVIDENCE", 70)
    assert "evidence conflicts" in with_isbn[2]["explanation"]


def test_without_an_isbn_nothing_changes(bindery):
    result = {"title": "Old Mars", "author": "James S. A. Corey"}
    metadata = {"title": "Old Mars", "author": "Gardner Dozois"}
    text = _front("OLD MARS", "GARDNER DOZOIS")

    verdict, _confidence, evidence = classify_identity(
        {**result, "isbn_evidence": isbn_evidence(10, [])}, metadata, text, [], [], text,
    )

    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert "isbn" not in evidence
