"""Series-aware title matching.

Catalogue titles often carry the series ("Mary, Mary: Alex Cross, Book 11") while
the book says only "Mary, Mary". The series-free title is an extra candidate;
every other identity requirement is unchanged.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import series_titles
from app.config import settings
from app.series_titles import bindery_series_names, expected_title_variants
from app.verification_engine import classify_identity


def _front(title: str, author: str) -> str:
    return f"{title}\n{author}\n\nCopyright notice. " + ("Chapter one begins here. " * 60)


@pytest.mark.parametrize(
    ("title", "series", "expected"),
    [
        ("Mary, Mary: Alex Cross, Book 11", [], ["Mary, Mary"]),
        ("Kiss the Girls (Alex Cross #2)", [], ["Kiss the Girls"]),
        ("The Lost Continent (Wings of Fire, Book 11)", [], ["The Lost Continent"]),
        ("The Hardy Boys 24 # The Short Wave Mystery", [], ["The Short Wave Mystery"]),
        ("Hardy Boys The Clue in the Embers", ["The Hardy Boys"], ["the clue in the embers"]),
        ("The Hardy Boys - THE DISAPPEARING FLOOR", ["The Hardy Boys"], ["the disappearing floor"]),
        # Without Bindery's series data, an unnumbered prefix is never guessed.
        ("Hardy Boys The Clue in the Embers", [], []),
        # Real subtitles without a series number are kept.
        ("Fear Itself: The Horror Fiction of Stephen King", [], []),
        ("Mary and Mr Eliot: a Sort of Love Story", [], []),
        ("From a Certain Point of View: The Empire Strikes Back", ["Star Wars"], []),
        # Nothing meaningful left once the series is removed.
        ("Alex Cross, Book 11", [], []),
        ("Rotten School", ["Rotten School"], []),
        ("Fang", [], []),
    ],
)
def test_series_free_title_is_derived_only_when_unambiguous(title, series, expected):
    variants = expected_title_variants(title, series)
    assert variants[0] == title
    assert variants[1:] == expected


def test_pilot_case_verifies_when_the_book_omits_the_series_tag():
    result = {"title": "Mary, Mary: Alex Cross, Book 11", "author": "James Patterson"}
    text = _front("MARY, MARY", "JAMES PATTERSON")

    verdict, confidence, evidence = classify_identity(
        result, {"title": "Mary, Mary", "author": "James Patterson"}, text, [], [], text,
    )

    assert (verdict, confidence) == ("VERIFIED_CORRECT", 99)
    assert evidence["expected"]["title"] == "Mary, Mary: Alex Cross, Book 11"
    assert evidence["expected"]["matchedTitle"] == "Mary, Mary"


def test_same_title_by_a_different_author_is_never_accepted():
    # Ed McBain also wrote "Mary, Mary" (87th Precinct).
    result = {"title": "Mary, Mary: Alex Cross, Book 11", "author": "James Patterson"}
    text = _front("MARY, MARY", "ED McBAIN")

    verdict, _confidence, _evidence = classify_identity(
        result, {"title": "Mary, Mary", "author": "Ed McBain"}, text, [], [], text,
    )

    assert verdict not in {"VERIFIED_CORRECT", "METADATA_ERROR"}


def test_a_wrong_book_is_still_identified_as_wrong():
    result = {"title": "Mary, Mary: Alex Cross, Book 11", "author": "James Patterson"}
    text = _front("MARY AND MR ELIOT: A SORT OF LOVE STORY", "MARY TREVELYAN")

    verdict, _confidence, _evidence = classify_identity(
        result,
        {"title": "Mary and Mr Eliot: a Sort of Love Story", "author": "Mary Trevelyan"},
        text, [], [], text,
    )

    assert verdict == "WRONG_CONTENT"


def test_hardy_boys_verify_with_bindery_series_and_the_pen_name(monkeypatch):
    monkeypatch.setattr(
        settings, "author_aliases", ["Leslie McFarlane = Franklin W. Dixon"],
    )
    text = _front("THE CLUE IN THE EMBERS", "FRANKLIN W. DIXON")
    metadata = {"title": "The Clue in the Embers", "author": "Franklin W. Dixon"}
    base = {"title": "Hardy Boys The Clue in the Embers", "author": "Leslie McFarlane"}

    with_series = classify_identity({**base, "series": ["The Hardy Boys"]}, metadata, text, [], [], text)
    without_series = classify_identity(base, metadata, text, [], [], text)

    assert with_series[:2] == ("VERIFIED_CORRECT", 99)
    assert without_series[0] != "VERIFIED_CORRECT"


def test_bindery_series_names_reads_series_read_only(tmp_path, monkeypatch):
    db_path = tmp_path / "bindery.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE series (id INTEGER PRIMARY KEY, foreign_id TEXT, title TEXT NOT NULL);
            CREATE TABLE series_books (
                series_id INTEGER, book_id INTEGER, position_in_series TEXT,
                primary_series INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO series VALUES (1, 'a', 'The Hardy Boys'), (2, 'b', 'Hardy Boys Digest');
            INSERT INTO series_books VALUES (1, 7, '24', 1), (2, 7, '', 0), (1, 8, '1', 1);
            """
        )
    monkeypatch.setattr(settings, "bindery_db", str(db_path))

    assert bindery_series_names(7) == ["The Hardy Boys", "Hardy Boys Digest"]
    assert bindery_series_names(99) == []
    assert bindery_series_names(None) == []


def test_bindery_series_names_is_empty_when_bindery_db_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bindery_db", str(tmp_path / "missing.db"))
    assert series_titles.bindery_series_names(7) == []


# --- series tags in the file's own title -------------------------------------


def test_file_title_with_numbered_series_tag_verifies():
    # Real Kindle header from the Unraid library (Wings of Fire #11).
    result = {"title": "Lost Continent", "author": "Tui T. Sutherland"}
    text = _front("THE LOST CONTINENT", "TUI T. SUTHERLAND")

    verdict, confidence, evidence = classify_identity(
        result,
        {"title": "The Lost Continent (Wings of Fire, Book 11)", "author": "Sutherland, Tui T."},
        text, [], [], text,
    )

    assert (verdict, confidence) == ("VERIFIED_CORRECT", 99)
    assert evidence["embedded"]["titleVariants"] == ["The Lost Continent"]


def test_unnumbered_file_series_tag_needs_bindery_series_data():
    # Real Kindle header: "The Feros (Vindico)" has no number to prove it is a series.
    base = {"title": "The Feros", "author": "Wesley King"}
    metadata = {"title": "The Feros (Vindico)", "author": "King, Wesley"}
    text = _front("THE FEROS", "WESLEY KING")

    with_series = classify_identity({**base, "series": ["Vindico"]}, metadata, text, [], [], text)
    without_series = classify_identity(base, metadata, text, [], [], text)

    assert with_series[:2] == ("VERIFIED_CORRECT", 99)
    assert without_series[0] != "VERIFIED_CORRECT"


def test_series_tagged_file_by_another_author_is_never_accepted():
    result = {"title": "Mary, Mary: Alex Cross, Book 11", "author": "James Patterson"}
    text = _front("MARY, MARY", "ED McBAIN")

    verdict, _confidence, _evidence = classify_identity(
        result, {"title": "Mary, Mary (87th Precinct, Book 37)", "author": "Ed McBain"},
        text, [], [], text,
    )

    assert verdict not in {"VERIFIED_CORRECT", "METADATA_ERROR"}
