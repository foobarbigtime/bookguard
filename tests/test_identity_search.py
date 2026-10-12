"""Second-pass evidence: tolerant identity and a search of the Bindery catalogue."""

from __future__ import annotations

import sqlite3

import pytest

from app import identity_search
from app.config import settings
from app.identity_search import (
    catalogue_candidates, second_pass, soft_author_match, soft_title_match,
)
from app.verification_engine import classify_identity


def test_author_forms_that_are_the_same_person():
    assert soft_author_match("Cressida Cowell", "C. Cowell")
    assert soft_author_match("Cressida Cowell", "Cowell, Cressida")
    assert soft_author_match("Cressida Cowell", "Cressida M. Cowell; Neil Gaiman")
    assert soft_author_match("J.K. Rowling", "Joanne Rowling")
    assert soft_author_match("Cressida Cowell", "Neil Gaiman and Cressida Cowell")


def test_a_surname_alone_or_another_person_is_not_a_match():
    assert not soft_author_match("Cressida Cowell", "Cowell")
    assert not soft_author_match("Cressida Cowell", "Dave Cowell")
    assert not soft_author_match("Cressida Cowell", "Cressida Smith")


def test_title_ignores_only_generic_suffixes_never_a_real_subtitle():
    assert soft_title_match("How to Steal a Dragon's Sword", "How to Steal a Dragon's Sword: A Novel")
    assert soft_title_match("The Hobbit", "Hobbit")
    assert not soft_title_match("The English Girl", "The English Girl: Spy Stories")
    assert not soft_title_match("Saga: Book One", "Saga: Book Two")
    assert not soft_title_match("Kill", "Kill Alex Cross")
    assert not soft_title_match("Mary, Mary: Book 11", "Mary, Mary: Book 12")


def evidence(file_title, file_author, *, title_in=False, author_in=False, file_title_in=True):
    return {
        "expected": {"title": "How to Steal a Dragon's Sword", "author": "Cressida Cowell"},
        "embedded": {"title": file_title, "author": file_author, "identifiers": []},
        "content": {
            "expected_signal": {"title_found": title_in, "author_found": author_in},
            "embedded_signal": {"title_found": file_title_in, "author_found": False},
        },
    }


RESULT = {"book_id": 1, "series": []}


def test_tolerant_match_verifies_only_with_text_support():
    ev = evidence("How to Steal a Dragon's Sword: A Novel", "C. Cowell", author_in=True)
    assert second_pass(RESULT, ev) == ("VERIFIED_CORRECT", 88)
    assert second_pass(RESULT, evidence("How to Steal a Dragon's Sword: A Novel", "C. Cowell")) is None


@pytest.fixture
def bindery(tmp_path, monkeypatch):
    path = tmp_path / "bindery.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER);
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT, path TEXT, size_bytes INTEGER);
            INSERT INTO authors VALUES (1, 'Cressida Cowell'), (2, 'Daniel Silva');
            INSERT INTO books VALUES (1, 'How to Steal a Dragon''s Sword', 1),
                                     (2, 'How to Cheat a Dragon''s Curse', 1),
                                     (3, 'The Secret Servant', 2);
            """
        )
    monkeypatch.setattr(settings, "bindery_db", str(path))
    return path


def test_catalogue_search_finds_the_one_other_book(bindery):
    found = catalogue_candidates("How to Cheat a Dragon's Curse", "Cressida Cowell", 1)
    assert [c["bookId"] for c in found] == [2] and found[0]["hasFile"] is False
    assert catalogue_candidates("How to Cheat a Dragon's Curse", "Cressida Cowell", 2) == []
    assert catalogue_candidates("The Secret Servant", "Someone Else", 1) == []


def test_file_that_is_another_book_becomes_wrong_content(bindery):
    ev = evidence("How to Cheat a Dragon's Curse", "Cressida Cowell")
    assert second_pass(RESULT, ev) == ("WRONG_CONTENT", 95)
    assert ev["actualBook"]["bookId"] == 2


def test_no_catalogue_match_stays_undecided_and_records_the_search(bindery):
    ev = evidence("A Book Nobody Has", "Unknown Person")
    assert second_pass(RESULT, ev) is None
    assert {"check": "catalogueSearch", "matches": 0} in ev["secondPass"]


def test_classify_identity_uses_the_second_pass(bindery):
    result = {"title": "How to Steal a Dragon's Sword", "author": "Cressida Cowell", "book_id": 1,
              "series": [], "isbn_evidence": {}}
    text = "How to Cheat a Dragon's Curse by Cressida Cowell. " * 5
    verdict, confidence, ev = classify_identity(
        result, {"title": "How to Cheat a Dragon's Curse", "author": "Cressida Cowell"}, text, [], [], text)
    assert verdict == "WRONG_CONTENT" and ev["actualBook"]["title"] == "How to Cheat a Dragon's Curse"


def test_old_undecided_verdicts_are_looked_at_again_but_new_ones_are_kept():
    from app.verifier import predates_second_pass
    old = {"verdict": "INSUFFICIENT_EVIDENCE", "evidence": {"content": {"expected_signal": {}}}}
    assert predates_second_pass(old)
    assert not predates_second_pass({**old, "evidence": {**old["evidence"], "secondPass": []}})
    assert not predates_second_pass({**old, "evidence": {**old["evidence"], "reasonCode": "TITLE_DESIGNATION_CONFLICT"}})
    assert not predates_second_pass({"verdict": "VERIFIED_CORRECT", "evidence": old["evidence"]})
    assert not predates_second_pass({"verdict": "INSUFFICIENT_EVIDENCE", "evidence": {"content": {}}})  # audiobook / outage
