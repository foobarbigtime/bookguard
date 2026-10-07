"""Catalogue suffixes from the October 7 audit must not hide real conflicts."""

import sqlite3

import pytest

from app.matcher import classify_ebook
from app.series_titles import ebook_title_match, expected_title_variants
from app.verification_engine import classify_identity


@pytest.mark.parametrize("title", [
    "Karen's Big Weekend (Baby-Sitters Little Sister #44)",
    "Karen's New Holiday (Baby-Sitters Little Sister #112)",
    "Kristy and the Baby Parade (the Baby-Sitters Club #45)",
    "Mary Anne vs. Logan (the Baby-Sitters Club #41)",
])
def test_numbered_catalogue_suffix_does_not_require_series_database_records(title):
    base = title.split(" (")[0]
    assert classify_ebook(title, "Ann M. Martin", {
        "title": base, "author": "Ann M. Martin",
    })[0] == "PASS"
    assert classify_ebook(base, "Ann M. Martin", {
        "title": title, "author": "Ann M. Martin",
    })[0] == "PASS"


@pytest.mark.parametrize(("title", "embedded", "series"), [
    ("Secret Servant, The (Gabriel Allon)", "The Secret Servant", ["Gabriel Allon"]),
    ("Dragonbreath Lair of the Bat Monster", "Lair of the Bat Monster", ["Dragonbreath"]),
    ("Goosebumps SlappyWorld - Please Do Not Feed the Weirdo", "Please Do Not Feed the Weirdo", ["Goosebumps SlappyWorld"]),
    ("Give Yourself Goosebumps - Deep in the Jungle of Doom", "Deep in the Jungle of Doom", ["Give Yourself Goosebumps"]),
])
def test_unnumbered_series_labels_require_recorded_series(title, embedded, series):
    assert not ebook_title_match(title, embedded)
    assert ebook_title_match(title, embedded, series)


@pytest.mark.parametrize(("title", "embedded"), [
    ("The Bad Guys #5", "The Bad Guys in Alien vs Bad Guys (The Bad Guys #6)"),
    ("Karen's Big Weekend (Baby-Sitters Little Sister #44)", "Karen's Big Weekend (Baby-Sitters Little Sister #45)"),
    ("The Hardy Boys 24 # The Short Wave Mystery", "The Hardy Boys 25 # The Short Wave Mystery"),
    ("Murder House: Part 5", "The Murder House"),
    ("The Murder House", "Murder House: Part 5"),
    ("Murder House: Part 5", "Murder House: Part 6"),
    ("Murder House: Volume 5", "The Murder House"),
    ("The Murder House", "Murder House: Book 5"),
    ("All-American Expedition", "All-American Murder"),
    ("NYPD Red 5", "NYPD Red 2"),
])
def test_series_normalization_cannot_clear_volume_or_part_conflicts(title, embedded):
    metadata = {"title": embedded, "author": "James Patterson"}
    assert classify_ebook(title, "James Patterson", metadata)[0] == "REVIEW"


@pytest.mark.parametrize("title", ["Murder House: Part 5", "Murder House (Volume 5)", "Murder House: Book 5"])
def test_bare_work_designations_are_not_removed(title):
    assert expected_title_variants(title) == [title]


def test_ebook_author_support_is_still_required():
    assert classify_ebook("Karen's Big Weekend (Baby-Sitters Little Sister #44)", "Ann M. Martin", {
        "title": "Karen's Big Weekend", "author": "James Patterson",
    })[0] == "REVIEW"


@pytest.mark.parametrize(("title", "embedded"), [
    ("Karen's Big Weekend (Baby-Sitters Little Sister #44)", "Karen's Big Weekend (Baby-Sitters Little Sister #45)"),
    ("Murder House: Part 5", "The Murder House"),
])
def test_front_page_base_title_cannot_prove_a_conflicting_designation(title, embedded):
    text = "Karen's Big Weekend\nThe Murder House\nJames Patterson\n" + "Chapter one begins. " * 100
    verdict, _, evidence = classify_identity(
        {"title": title, "author": "James Patterson"},
        {"title": embedded, "author": "James Patterson"},
        text, [], [], text,
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert evidence["reasonCode"] == "TITLE_DESIGNATION_CONFLICT"


def test_correct_numbered_suffix_still_verifies_from_front_page():
    text = "Karen's Big Weekend\nAnn M. Martin\n" + "Chapter one begins. " * 100
    verdict, confidence, _ = classify_identity(
        {"title": "Karen's Big Weekend (Baby-Sitters Little Sister #44)", "author": "Ann M. Martin"},
        {"title": "Karen's Big Weekend", "author": "Ann M. Martin"},
        text, [], [], text,
    )
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 99)


def test_scan_uses_bindery_recorded_series(tmp_path, monkeypatch):
    from app import scanner
    from app.config import settings

    db_path = tmp_path / "bindery.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE series_books (series_id INTEGER, book_id INTEGER, primary_series INTEGER);
            INSERT INTO series VALUES (1, 'Dragonbreath');
            INSERT INTO series_books VALUES (1, 7, 1);
        """)
    monkeypatch.setattr(settings, "bindery_db", str(db_path))
    monkeypatch.setattr(scanner, "ebook_metadata", lambda target: {
        "title": "Lair of the Bat Monster", "author": "Ursula Vernon",
    })
    monkeypatch.setattr(scanner, "ebook_languages", lambda target: {"languages": []})
    ebook = tmp_path / "book.epub"
    ebook.write_bytes(b"metadata supplied by the test")
    row = {
        "book_id": 7, "format": "ebook", "stored_path": str(ebook),
        "title": "Dragonbreath Lair of the Bat Monster", "author": "Ursula Vernon",
    }
    assert scanner._scan_one(row)["classification"] == "PASS"
    monkeypatch.setattr(settings, "bindery_db", str(tmp_path / "unavailable.db"))
    assert scanner._scan_one(row)["classification"] == "REVIEW"
