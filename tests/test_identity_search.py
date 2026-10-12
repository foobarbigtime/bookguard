"""Second-pass evidence: tolerant identity and a search of the Bindery catalogue."""

from __future__ import annotations

import sqlite3

import pytest

from app.config import settings
from app.identity_search import (
    author_match_level, catalogue_candidates, second_pass, soft_author_match, soft_title_match,
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
            "embedded_signal": {"title_found": file_title_in, "author_found": False, "strong_identity": True},
        },
    }


RESULT = {"book_id": 1, "series": []}


def test_tolerant_match_verifies_only_with_text_support():
    ev = evidence("How to Steal a Dragon's Sword: A Novel", "C. Cowell", author_in=True)
    assert second_pass(RESULT, ev) == ("VERIFIED_CORRECT", 88)
    assert second_pass(RESULT, evidence("How to Steal a Dragon's Sword: A Novel", "C. Cowell")) is None


@pytest.mark.parametrize("file_author", ["Cressida Cowell", "C. Cowell"])
def test_shared_author_alone_never_settles_the_second_pass(file_author):
    ev = evidence("How to Steal a Dragon's Sword", file_author,
                  author_in=True, file_title_in=False)
    assert second_pass(RESULT, ev) is None


@pytest.fixture
def bindery(tmp_path, monkeypatch):
    path = tmp_path / "bindery.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER, excluded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT, path TEXT, size_bytes INTEGER);
            INSERT INTO authors VALUES (1, 'Cressida Cowell'), (2, 'Daniel Silva');
            INSERT INTO books(id, title, author_id) VALUES (1, 'How to Steal a Dragon''s Sword', 1),
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
    assert second_pass(RESULT, ev) == ("WRONG_CONTENT", 85)   # title and author only: no ISBN confirms it
    assert ev["actualBook"]["bookId"] == 2 and ev["secondPassCatalogue"] == {"isbnCorroborated": False}


def test_an_isbn_naming_the_same_other_book_raises_confidence(bindery):
    ev = evidence("How to Cheat a Dragon's Curse", "Cressida Cowell")
    ev["isbn"] = {"isbns": ["9780316085045"], "expectedMatch": False, "otherOwners": [{"bookId": 2}]}
    assert second_pass(RESULT, ev) == ("WRONG_CONTENT", 95)
    assert ev["secondPassCatalogue"] == {"isbnCorroborated": True}


def test_an_isbn_naming_a_third_book_blocks_the_verdict(bindery):
    ev = evidence("How to Cheat a Dragon's Curse", "Cressida Cowell")
    ev["isbn"] = {"isbns": ["9780316085045"], "expectedMatch": False, "otherOwners": [{"bookId": 3}]}
    assert second_pass(RESULT, ev) is None
    assert {"check": "isbnNamesAnotherBook"} in ev["secondPass"]


def test_a_file_that_does_not_open_with_its_own_identity_is_not_called_another_book(bindery):
    ev = evidence("How to Cheat a Dragon's Curse", "Cressida Cowell")
    ev["content"]["embedded_signal"]["strong_identity"] = False
    assert second_pass(RESULT, ev) is None


def test_excluded_bindery_books_are_never_the_other_book(bindery):
    with sqlite3.connect(bindery) as conn:
        conn.execute("UPDATE books SET excluded = 1 WHERE id = 2")
    assert catalogue_candidates("How to Cheat a Dragon's Curse", "Cressida Cowell", 1) == []


def test_accents_and_word_order_are_handled_by_the_matcher_not_a_sql_prefilter(bindery):
    with sqlite3.connect(bindery) as conn:
        conn.execute("INSERT INTO books(id, title, author_id) VALUES (10, 'How to Cheat a Dragon''s Curse', 1)")
    # A second, identical Bindery entry (a duplicate) makes the target ambiguous: nothing is declared.
    ev = evidence("How to Cheat a Dragon's Curse", "Cressida Cowell")
    assert second_pass(RESULT, ev) is None
    assert {"check": "catalogueSearch", "matches": 2} in ev["secondPass"]


def test_a_bindery_read_error_is_not_final(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bindery_db", str(tmp_path / "missing.db"))
    ev = evidence("How to Cheat a Dragon's Curse", "Cressida Cowell")
    assert second_pass(RESULT, ev) is None
    assert ev["secondPassIncomplete"] is True


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


# --- author matching: structured names, found by adversarial review ---------------------------


def test_different_people_sharing_a_surname_never_match():
    for expected, credits in [
        ("Michael Martin", "Ann M. Martin"), ("Roger Martin", "George R. R. Martin"),
        ("Kevin Dick", "Philip K. Dick"), ("Jennifer Maas", "Sarah J. Maas"),
        ("Mary Higgins Clark", "Mary Jane Clark"), ("Robert B. Parker", "Robert M. Parker Jr."),
        ("George R. R. Martin", "George W. Martin"), ("J. Smith", "Smith Jr."), ("A. Smith", "Smith et al."),
        ("Ann Martin", "Martin"), ("Cressida Cowell", "Dave Cowell"),
    ]:
        assert author_match_level(expected, credits) == "", (expected, credits)


def test_same_person_forms_are_exact_or_soft():
    from app.identity_search import author_match_level
    assert author_match_level("Cressida Cowell", "Cowell, Cressida") == "exact"
    assert author_match_level("Ann M. Martin", "Ann M. Martin; Neil Gaiman") == "exact"
    assert author_match_level("Ann M. Martin", "Martin, Ann M.") == "exact"
    assert author_match_level("Cressida Cowell", "C. Cowell") == "soft"
    assert author_match_level("Daniel Silva", "D. Silva") == "soft"
    assert author_match_level("Ann M. Martin", "Ann Martin") == "soft"   # a middle initial omitted


def _soft_author_evidence(author_found):
    return {
        "expected": {"title": "How to Steal a Dragon's Sword", "author": "J. K. Rowling"},
        "embedded": {"title": "How to Steal a Dragon's Sword", "author": "Jack Rowling", "identifiers": []},
        "content": {"expected_signal": {"title_found": True, "author_found": author_found},
                    "embedded_signal": {"title_found": True}},
    }


def test_a_soft_author_match_needs_the_assigned_author_in_the_book():
    assert second_pass({"book_id": 1, "series": []}, _soft_author_evidence(False)) is None
    assert second_pass({"book_id": 1, "series": []}, _soft_author_evidence(True)) == ("VERIFIED_CORRECT", 88)


def test_the_catalogue_search_accepts_only_exact_authors(bindery):
    assert catalogue_candidates("How to Cheat a Dragon's Curse", "C. Cowell", 1) == []
    assert [c["bookId"] for c in catalogue_candidates("How to Cheat a Dragon's Curse", "Cowell, Cressida", 1)] == [2]


def test_second_pass_verdicts_from_older_rules_are_looked_at_again():
    from app.identity_search import SECOND_PASS_VERSION
    from app.verifier import predates_second_pass
    base = {"content": {"expected_signal": {}}, "secondPass": [{"check": "catalogueSearch", "matches": 1}]}
    current = {**base, "secondPassVersion": SECOND_PASS_VERSION}
    for verdict in ("WRONG_CONTENT", "VERIFIED_CORRECT", "INSUFFICIENT_EVIDENCE"):
        assert predates_second_pass({"verdict": verdict, "evidence": base})                          # no version
        assert predates_second_pass({"verdict": verdict, "evidence": {**base, "secondPassVersion": SECOND_PASS_VERSION - 1}})
        assert not predates_second_pass({"verdict": verdict, "evidence": current})
    assert not predates_second_pass({"verdict": "WRONG_CONTENT", "evidence": {"content": base["content"]}})  # first-pass verdict
    # A Bindery read failure during the second pass is never cached as final.
    assert predates_second_pass({"verdict": "INSUFFICIENT_EVIDENCE", "evidence": {**current, "secondPassIncomplete": True}})
    # An undecided ebook verdict from before the second pass existed, but not a volume conflict.
    legacy = {"content": {"expected_signal": {}}}
    assert predates_second_pass({"verdict": "INSUFFICIENT_EVIDENCE", "evidence": legacy})
    assert not predates_second_pass({"verdict": "INSUFFICIENT_EVIDENCE",
                                     "evidence": {**legacy, "reasonCode": "TITLE_DESIGNATION_CONFLICT"}})
    assert not predates_second_pass({"verdict": "INSUFFICIENT_EVIDENCE", "evidence": {"content": {}}})  # audiobook / outage


# --- findings from the adversarial review of the second pass ---------------------------------


def test_division_words_range_labels_and_several_numbers_are_never_series_labels():
    for label in ("Act 2", "Level 2", "Season 3", "Chapter 5", "Volume 3", "Canto 5", "Part Two", "Edition 2"):
        assert not soft_title_match(f"Hamlet ({label})", "Hamlet"), label
    # An omnibus, a range, "N of M" or a fraction is not one volume.
    for label in ("Dune Chronicles, 1-3", "Dune Chronicles, 1/3", "Book 2 of 5", "Series 1.5", "Series 1 & 2"):
        assert not soft_title_match(f"Dune ({label})", "Dune"), label
    assert soft_title_match("Dune (Dune Chronicles, 1)", "Dune")


def test_volume_numbers_are_compared_on_the_original_titles():
    assert not soft_title_match("Dune (Dune Chronicles, 1)", "Dune (Dune Chronicles, 2)")
    assert not soft_title_match("Stacey (the Baby-Sitters Club, 8)", "Stacey (the Baby-Sitters Club, 9)", ["Baby-Sitters Club"])


def test_word_order_and_non_latin_titles_never_count_as_equal():
    assert not soft_title_match("Part Two Book One", "Book One Part Two")
    assert not soft_title_match("War of the Worlds", "Worlds of the War")
    assert not soft_title_match("日本語の本", "中文的书")
    assert soft_title_match("Secret Servant, The (Gabriel Allon)", "The Secret Servant", ["Gabriel Allon"])


def test_a_crafted_title_cannot_stall_the_matcher():
    import time
    start = time.monotonic()
    for hostile in ("A (" * 5000, "(" * 20000 + "x", "Title (" + "a " * 10000, "x" * 100000 + ")"):
        soft_title_match(hostile, "Title")
        soft_title_match("Title", hostile)
    assert time.monotonic() - start < 2.0


def test_placeholder_authors_and_comma_lists_and_compound_surnames():
    for placeholder in ("Unknown", "Various", "Anonymous", "Administrator", "Microsoft Office User", "Unknown Author"):
        assert author_match_level(placeholder, placeholder) == "", placeholder
        assert author_match_level("Ann M. Martin", placeholder) == ""
    # "Ann Martin, Neil Gaiman" is a list of two people, never "Martin, Neil".
    assert author_match_level("Neil Martin", "Ann Martin, Neil Gaiman") == ""
    assert author_match_level("Neil Gaiman", "Ann Martin, Neil Gaiman") == "exact"
    assert author_match_level("Ann M. Martin", "Martin, Ann M.") == "exact"
    # A compound surname is one word; Dr./III are not names.
    assert author_match_level("Mary Jones", "Mary Smith-Jones") == ""
    assert author_match_level("Dr. Anna Johnson", "Dr. Ben Johnson") == ""
    assert author_match_level("Ann Martin", "Ann Martin III") == "exact"
