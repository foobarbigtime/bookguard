"""Provenance: Bindery's download names as independent evidence, using real library examples."""

from __future__ import annotations

import sqlite3

import pytest

from app.config import settings
from app.identity_search import author_match_level, second_pass
from app.provenance import (
    core_title, designations, parse_release_name, provenance_proof, releases_for_book,
)


def test_release_names_from_a_real_library_parse():
    r = parse_release_name("Andy Griffiths - [Schooling Around 04] - Robot Riot! (retail) (epub)")
    assert (r.kind, r.author, r.series, r.number, r.title) == ("book", "Andy Griffiths", "Schooling Around", "4", "Robot Riot!")
    r = parse_release_name("Andy Griffiths - The Cat on the Mat is Flat (retail) (epub)")
    assert (r.kind, r.author, r.title, r.series) == ("book", "Andy Griffiths", "The Cat on the Mat is Flat", "")
    r = parse_release_name("Andy Griffiths - [Treehouse 10] - The 130-Storey (Story) Treehouse (retail) (epub)")
    assert r.title == "The 130-Storey (Story) Treehouse"  # a real title parenthetical is kept
    assert parse_release_name("VA-The_Bad_Guys_2-OST-WEB-2025-MARR").kind == "music"
    assert parse_release_name("The Bad Guys #5").kind == "unknown"
    assert parse_release_name("").kind == "unknown"


def test_core_title_removes_labels_but_never_a_subtitle():
    assert core_title("Robot Riot Andy Griffiths", "Andy Griffiths") == "robot riot"
    assert core_title("The 39-Storey Treehouse (The Treehouse Books)") == "39 storey treehouse"
    assert core_title("Abby's Book (the Baby-Sitters Club Portrait Collection)") == core_title("Abbys Book") == "abbys book"
    assert core_title("The English Girl") != core_title("The English Girl: Spy Stories")
    assert designations("Boy-Crazy Stacey (the Baby-Sitters Club, 8)") == {"8"}


EXPECTED = {"title": "Robot Riot Andy Griffiths", "author": "Andy Griffiths"}
EMBEDDED = {"title": "Robot Riot!", "author": "Andy Griffiths"}
SIGNALS = {"expectedAuthorInText": True}
RELEASE = parse_release_name("Andy Griffiths - [Schooling Around 04] - Robot Riot! (retail) (epub)")


def proof(expected=EXPECTED, embedded=EMBEDDED, signals=SIGNALS, releases=(RELEASE,)):
    return provenance_proof(expected, embedded, signals, releases, author_match_level)


def test_three_sources_agreeing_is_proof():
    result = proof()
    assert result and result["release"].startswith("Andy Griffiths - [Schooling Around 04]")


def test_every_leg_is_required():
    assert proof(signals={}) is None                                              # no support in the book
    assert proof(releases=()) is None                                             # no download name
    assert proof(embedded={"title": "Robot Riot!", "author": "Someone Else"}) is None
    assert proof(embedded={"title": "Robot Rampage", "author": "Andy Griffiths"}) is None
    assert proof(releases=(parse_release_name("Andy Griffiths - Robot Rampage (retail) (epub)"),)) is None
    assert proof(releases=(parse_release_name("Other Person - Robot Riot! (retail) (epub)"),)) is None
    assert proof(releases=(parse_release_name("VA-The_Bad_Guys_2-OST-WEB-2025-MARR"),)) is None


def test_other_forms_volumes_and_subtitles_never_agree():
    assert proof(embedded={"title": "Robot Riot! (Graphic Novel)", "author": "Andy Griffiths"}) is None
    assert proof(expected={"title": "Robot Riot (Abridged)", "author": "Andy Griffiths"}) is None
    assert proof(expected={"title": "Robot Riot: The Sequel", "author": "Andy Griffiths"}) is None
    vol = parse_release_name("Ann M Martin - [Baby-sitters Club 09] - Stacey (retail) (epub)")
    assert proof({"title": "Stacey (the Baby-Sitters Club, 8)", "author": "Ann M. Martin"},
                 {"title": "Stacey", "author": "Ann M. Martin"}, SIGNALS, (vol,)) is None
    ok = parse_release_name("Ann M Martin - [Baby-sitters Club 08] - Stacey (retail) (epub)")
    assert proof({"title": "Stacey (the Baby-Sitters Club, 8)", "author": "Ann M. Martin"},
                 {"title": "Stacey", "author": "Ann M. Martin"}, SIGNALS, (ok,))


@pytest.fixture
def bindery(tmp_path, monkeypatch):
    path = tmp_path / "bindery.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE history (id INTEGER PRIMARY KEY, book_id INTEGER, event_type TEXT, source_title TEXT, data TEXT, created_at TEXT);
            INSERT INTO history(book_id, event_type, source_title) VALUES
              (7, 'grabbed', 'Andy Griffiths - [Schooling Around 04] - Robot Riot! (retail) (epub)'),
              (7, 'bookImported', 'Andy Griffiths - [Schooling Around 04] - Robot Riot! (retail) (epub)'),
              (7, 'downloadFailed', 'Andy Griffiths - Something Else (retail) (epub)'),
              (8, 'bookImported', 'VA-The_Bad_Guys_2-OST-WEB-2025-MARR');
            """
        )
    monkeypatch.setattr(settings, "bindery_db", str(path))
    return path


def test_history_is_read_without_failed_events_and_without_duplicates(bindery):
    found = releases_for_book(7)
    assert [r.title for r in found] == ["Robot Riot!"]
    assert found[0].event == "bookImported"
    assert releases_for_book(0) == [] and releases_for_book(999) == []


def test_second_pass_verifies_from_the_download_name_and_records_it(bindery):
    ev = {"expected": dict(EXPECTED), "embedded": dict(EMBEDDED),
          "content": {"expected_signal": {"title_found": False, "author_found": True},
                      "embedded_signal": {"title_found": True}}}
    assert second_pass({"book_id": 7, "series": []}, ev) == ("VERIFIED_CORRECT", 90)
    assert ev["proof"]["release"].startswith("Andy Griffiths") and "Three independent sources" in ev["explanation"]
    assert ev["provenance"]["releases"][0]["title"] == "Robot Riot!"


def test_a_soundtrack_release_is_recorded_as_not_a_book_and_proves_nothing(bindery):
    ev = {"expected": {"title": "The Bad Guys #5", "author": "Aaron Blabey"},
          "embedded": {"title": "The Bad Guys", "author": "Aaron Blabey"},
          "content": {"expected_signal": {"author_found": True}, "embedded_signal": {"title_found": True}}}
    assert second_pass({"book_id": 8, "series": []}, ev) is None
    assert ev["provenance"]["notABook"] == ["VA-The_Bad_Guys_2-OST-WEB-2025-MARR"]
