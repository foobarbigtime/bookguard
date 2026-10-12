"""Second-pass evidence: tolerant identity and a search of the Bindery catalogue."""

from __future__ import annotations

import sqlite3

import pytest

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


def test_conflicting_isbn_for_the_assigned_book_is_not_overridden(bindery):
    ev = evidence("How to Cheat a Dragon's Curse", "Cressida Cowell")
    ev["isbn"] = {"expectedMatch": True}
    assert second_pass(RESULT, ev) is None
    assert "actualBook" not in ev


def test_uniqueness_is_checked_across_the_whole_catalogue(bindery):
    with sqlite3.connect(bindery) as conn:
        conn.executemany("INSERT INTO books(id, title, author_id) VALUES (?, ?, 2)",
                         [(100 + i, f"Dragon Filler {i}") for i in range(400)])
        # A second match for the file's identity, far past any 300-row window.
        conn.execute("INSERT INTO books(id, title, author_id) VALUES (900, 'How to Cheat a Dragon''s Curse', 1)")
    found = catalogue_candidates("How to Cheat a Dragon's Curse", "Cressida Cowell", 1)
    assert sorted(c["bookId"] for c in found) == [2, 900]
    ev = evidence("How to Cheat a Dragon's Curse", "Cressida Cowell")
    assert second_pass(RESULT, ev) is None  # two candidates: left undecided


def test_graphic_novel_and_other_forms_are_different_books():
    plain = "Good-Bye Stacey, Good-Bye"
    assert not soft_title_match(plain, "Good-Bye Stacey, Good-Bye (The Baby-Sitters Club Graphic Novel #11)")
    assert not soft_title_match(plain, "Good-Bye Stacey, Good-Bye (Graphic Novel)")
    assert not soft_title_match("Dragon Sword (Abridged)", "Dragon Sword")
    assert not soft_title_match("Dragon Sword", "Dragon Sword: The Illustrated Edition")
    assert soft_title_match("Dragon Sword (Abridged)", "Dragon Sword: Abridged")


def test_second_pass_verdicts_from_older_rules_are_looked_at_again():
    from app.verifier import predates_second_pass
    base = {"content": {"expected_signal": {}}, "secondPass": [{"check": "catalogueSearch", "matches": 1}]}
    assert predates_second_pass({"verdict": "WRONG_CONTENT", "evidence": base})
    assert predates_second_pass({"verdict": "VERIFIED_CORRECT", "evidence": {**base, "secondPassVersion": 1}})
    assert not predates_second_pass({"verdict": "WRONG_CONTENT", "evidence": {**base, "secondPassVersion": 2}})
    assert not predates_second_pass({"verdict": "WRONG_CONTENT", "evidence": {"content": base["content"]}})  # first-pass verdict


def test_numbered_series_label_is_ignored_even_when_bindery_lists_no_series():
    assert soft_title_match("Boy-Crazy Stacey (the Baby-Sitters Club, 8)", "Boy-Crazy Stacey")
    assert soft_title_match("Mary, Mary (Alex Cross #11)", "Mary, Mary")
    # ...but never a different volume, an edition marker, or a part.
    assert not soft_title_match("Stacey (the Baby-Sitters Club, 8)", "Stacey (the Baby-Sitters Club, 9)")
    assert not soft_title_match("Dragon Sword (Graphic Novel)", "Dragon Sword")
    assert not soft_title_match("Dragon Sword (Abridged)", "Dragon Sword")
    assert not soft_title_match("Dune (Part One)", "Dune")


def test_unnumbered_label_needs_the_isbn_to_back_it():
    assert not soft_title_match("The Feros (Vindico)", "The Feros")
    assert not soft_title_match("Summer Before (the Baby-Sitters Club)", "The Summer Before")
    assert soft_title_match("Summer Before (the Baby-Sitters Club)", "The Summer Before", allow_unnumbered=True)
    assert not soft_title_match("Dragon Sword (Graphic Novel)", "Dragon Sword", allow_unnumbered=True)


def test_unnumbered_series_book_verifies_with_its_isbn():
    ev = {
        "expected": {"title": "Summer Before (the Baby-Sitters Club)", "author": "Ann M. Martin"},
        "embedded": {"title": "The Summer Before", "author": "Ann M. Martin", "identifiers": []},
        "content": {"expected_signal": {"title_found": True, "author_found": True},
                    "embedded_signal": {"title_found": True}},
    }
    ev["content"]["expected_signal"]["title_found"] = False
    assert second_pass({"book_id": 1, "series": []}, ev) is None          # label not in the text, no ISBN
    ev["isbn"] = {"expectedMatch": True}
    assert second_pass({"book_id": 1, "series": []}, ev) == ("VERIFIED_CORRECT", 95)


def test_label_on_bindery_title_is_accepted_when_the_book_prints_the_whole_title():
    ev = {
        "expected": {"title": "Summer Before (the Baby-Sitters Club)", "author": "Ann M. Martin"},
        "embedded": {"title": "The Summer Before", "author": "Ann M. Martin", "identifiers": []},
        "content": {"expected_signal": {"title_found": True, "author_found": True},
                    "embedded_signal": {"title_found": True}},
        "isbn": {"isbns": ["9781338094589"], "expectedMatch": False, "otherOwners": []},
    }
    assert second_pass({"book_id": 1, "series": []}, ev) == ("VERIFIED_CORRECT", 88)


def test_label_on_the_files_side_still_needs_the_isbn():
    ev = {
        "expected": {"title": "The Feros", "author": "Wesley King"},
        "embedded": {"title": "The Feros (Vindico)", "author": "King, Wesley", "identifiers": []},
        "content": {"expected_signal": {"title_found": True, "author_found": True},
                    "embedded_signal": {"title_found": True}},
    }
    assert second_pass({"book_id": 1, "series": []}, ev) is None


def test_books_with_matching_author_isbn_and_title_in_text_now_verify():
    result = {"title": "Boy-Crazy Stacey (the Baby-Sitters Club, 8)", "author": "Ann M. Martin",
              "book_id": 1, "series": [], "isbn_evidence": {}}
    text = "Boy-Crazy Stacey (the Baby-Sitters Club, 8) by Ann M. Martin. " + "Chapter one. " * 200
    # Advertisement-like position: the title only appears deep in the text.
    far = "x " * 70000 + text
    verdict, confidence, evidence = classify_identity(
        result, {"title": "Boy-Crazy Stacey", "author": "Ann M. Martin"}, far, [], [], "front matter")
    assert verdict in {"VERIFIED_CORRECT"} and confidence >= 88


def test_qualifiers_that_do_not_change_the_text_are_ignored_on_either_side():
    plain = "Harry Potter and the Chamber of Secrets"
    for label in ("Full-Cast Edition", "Kindle Edition", "Movie Tie-In Edition", "Special Edition", "Dramatized"):
        assert soft_title_match(f"{plain} ({label})", plain), label
        assert soft_title_match(plain, f"{plain} ({label})"), label
    # ...but not a translation, a different form, or an arbitrary bracket.
    for label in ("Spanish Edition", "Graphic Novel", "Abridged", "Illustrated Edition", "Vindico", "Part Two"):
        assert not soft_title_match(f"{plain} ({label})", plain), label


def test_full_cast_edition_ebook_verifies_when_the_author_is_in_the_book():
    ev = {
        "expected": {"title": "Harry Potter and the Chamber of Secrets (Full-Cast Edition)", "author": "J.K. Rowling"},
        "embedded": {"title": "Harry Potter and the Chamber of Secrets", "author": "J.K. Rowling", "identifiers": []},
        "content": {"expected_signal": {"title_found": False, "author_found": True},
                    "embedded_signal": {"title_found": True}},
    }
    assert second_pass({"book_id": 1, "series": []}, ev) == ("VERIFIED_CORRECT", 88)
