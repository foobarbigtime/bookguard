"""Books Bindery lists twice: proven one book, then fixed (hidden or joined) automatically."""

from __future__ import annotations

import sqlite3

import pytest

from app import duplicates
from app.actions import ActionError
from app.config import settings
from app.db import init_local_db, recent_cleanup_actions

BOOKS = [
    # id, author, title, year, ebook, audiobook, excluded, language
    (1, 1, "Nightingale", "2015-02-03", "/b/Nightingale.epub", "", 0, "eng"),
    (2, 1, "The Nightingale", "2015", "", "", 0, ""),
    (3, 2, "The Client", "1993", "/b/The Client.epub", "", 0, ""),
    (4, 2, "Client", "", "", "/a/Client", 0, ""),
    (5, 2, "Time to Kill", "", "/b/Time to Kill.mobi", "", 0, ""),
    (6, 2, "A Time to Kill", "1989", "/b/A Time to Kill.epub", "", 0, ""),
    (7, 3, "Superman / Shazam!", "", "", "", 0, ""),
    (8, 3, "Superman/Shazam!", "", "", "", 0, ""),
    (9, 4, "Lost", "2020", "/b/Lost.epub", "", 0, ""),
    (10, 4, "The Lost", "2014", "", "", 0, ""),
    (11, 5, "Hank (Book 1)", "", "/b/Hank1.epub", "", 0, ""),
    (12, 5, "Hank (Book 2)", "", "", "", 0, ""),
    (13, 6, "战争 1", "", "/b/war.epub", "", 0, ""),
    (14, 6, "和平 1", "", "", "", 0, ""),
    (15, 7, "Order", "", "/b/Order.epub", "", 0, "eng"),
    (16, 7, "The Order", "", "", "", 0, "ger"),  # another language: maybe a translation
    (17, 8, "Patron Saint of Liars", "2025", "/b/Patron.epub", "", 0, ""),
    (18, 8, "The Patron Saint of Liars", "1992", "", "", 0, ""),  # wrong year, but the same ISBN
    (19, 9, "Fang", "", "/b/Fang.epub", "", 0, ""),
    (20, 9, "The Fang", "", "", "", 0, ""),  # a different place in the same series
    (21, 10, "Dark", "", "/b/Dark.epub", "", 0, ""),
    (23, 11, "Witness", "2000", "/b/Witness.epub", "", 0, ""),
    (24, 11, "The Witness", "2000", "", "", 0, ""),  # shares an ISBN with 23
    (25, 11, "Witness!", "2020", "", "", 0, ""),  # nothing ties it to 23 but the title
    (26, 12, "Gioco", "", "/b/Gioco.epub", "", 0, "ita"),
    (27, 12, "The Gioco", "", "", "", 0, "por"),  # languages outside the short list still differ
    (22, 10, "The Dark", "", "", "", 1, ""),  # already excluded: Bindery doesn't show it
]


@pytest.fixture
def bindery(tmp_path, monkeypatch):
    db = tmp_path / "bindery.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT, release_date TEXT,
                ebook_file_path TEXT NOT NULL DEFAULT '', audiobook_file_path TEXT NOT NULL DEFAULT '',
                excluded INTEGER NOT NULL DEFAULT 0, language TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '', asin TEXT NOT NULL DEFAULT '');
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT, path TEXT);
            CREATE TABLE editions (id INTEGER PRIMARY KEY, book_id INTEGER, isbn_13 TEXT, isbn_10 TEXT, asin TEXT);
            CREATE TABLE series (id INTEGER PRIMARY KEY, title TEXT);
            CREATE TABLE series_books (series_id INTEGER, book_id INTEGER, position_in_series TEXT);
        """)
        conn.executemany("INSERT INTO authors VALUES (?, ?)", [
            (1, "Kristin Hannah"), (2, "John Grisham"), (3, "Jeff Parker"), (4, "James Patterson"),
            (5, "Steven Campbell"), (6, "Someone"), (7, "Daniel Silva"), (8, "Ann Patchett"),
            (9, "James Patterson"), (10, "Lemony Snicket"), (11, "Nora Roberts"), (12, "Elena Ferrante")])
        conn.executemany("INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '')", BOOKS)
        conn.executemany("INSERT INTO book_files(book_id, format, path) VALUES (?, ?, ?)", [
            (b[0], "ebook", b[4]) for b in BOOKS if b[4]] + [(b[0], "audiobook", b[5]) for b in BOOKS if b[5]])
        conn.executemany("INSERT INTO editions(book_id, isbn_13) VALUES (?, ?)",
                         [(17, "978-0-06-054075-3"), (18, "9780060540753"), (23, "9780000000017"),
                          (24, "9780000000017")])
        conn.execute("INSERT INTO series VALUES (1, 'Maximum Ride')")
        conn.executemany("INSERT INTO series_books VALUES (1, ?, ?)", [(19, "8"), (20, "6")])
    monkeypatch.setattr(settings, "bindery_db", str(db))
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "ebook_bindery_prefix", "/b")
    monkeypatch.setattr(settings, "audiobook_bindery_prefix", "/a")
    monkeypatch.setattr(settings, "allow_actions", True)
    init_local_db()
    return db


def _by_title(found):
    return {g["entries"][0]["title"]: g for g in found}


class Client:
    """Bindery's API, changing the fake database the way Bindery would."""

    def __init__(self, db, preview="move"):
        self.db, self.preview, self.excluded, self.moved = db, preview, [], []

    def exclude_book(self, book_id):
        self.excluded.append(book_id)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE books SET excluded = 1 WHERE id = ?", (book_id,))
        return {"ok": True}

    def preview_manual_reassignment(self, path, book_id, *, file_format):
        root = "/a" if file_format == "audiobook" else "/b"
        return {"status": self.preview, "destination": f"{root}/John Grisham/The Client (1993)"}

    def reassign_manual_import(self, path, book_id, *, file_format):
        self.moved.append((path, book_id))
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE book_files SET book_id = ?, path = ? WHERE path = ?",
                         (book_id, "/a/John Grisham/The Client (1993)", path))
            conn.execute("UPDATE books SET audiobook_file_path = '' WHERE audiobook_file_path = ?", (path,))


def test_each_pair_is_proven_or_left_with_the_reason(bindery):
    found = _by_title(duplicates.find_duplicates())

    assert set(found) == {"Nightingale", "The Client", "Time to Kill", "Superman / Shazam!", "Lost", "Order",
                          "Patron Saint of Liars", "Fang", "Witness", "Gioco"}
    assert (found["Nightingale"]["keep"], found["Nightingale"]["hideable"]) == (1, [2])
    assert found["Superman / Shazam!"]["kind"] == "ALL_EMPTY" and found["Superman / Shazam!"]["hideable"] == [8]
    assert found["The Client"]["kind"] == "SPLIT" and found["The Client"]["moves"] == [
        {"from": 4, "format": "audiobook", "path": "/a/Client"}]
    assert found["Time to Kill"]["kind"] == "TWO_COPIES" and not found["Time to Kill"]["moves"]
    assert found["Patron Saint of Liars"]["hideable"] == [18]  # the shared ISBN outweighs the wrong year
    assert found["Patron Saint of Liars"]["proof"] == "the same ISBN or ASIN"
    assert "the year 2014" in found["Lost"]["blocked"] and not found["Lost"]["hideable"]
    assert "different languages" in found["Order"]["blocked"]
    assert "different numbers (6 and 8)" in found["Fang"]["blocked"]
    assert found["Witness"]["hideable"] == [24] and "Witness!" in found["Witness"]["blocked"]
    assert "different languages" in found["Gioco"]["blocked"] and not found["Gioco"]["hideable"]


def test_an_older_editions_table_without_asin_still_works(bindery):
    with sqlite3.connect(bindery) as conn:
        conn.executescript("""
            CREATE TABLE editions_old AS SELECT id, book_id, isbn_13, isbn_10 FROM editions;
            DROP TABLE editions; ALTER TABLE editions_old RENAME TO editions;
        """)
    found = _by_title(duplicates.find_duplicates())
    assert found["Patron Saint of Liars"]["hideable"] == [18]


def test_hiding_excludes_only_a_proven_empty_entry_and_records_it(bindery):
    client = Client(bindery)

    assert "The Nightingale" in duplicates.hide_empty(2, client)
    assert client.excluded == [2]
    action = recent_cleanup_actions(1)[0]
    assert (action["action_kind"], action["status"], action["book_id"]) == ("HIDE_DUPLICATE", "applied", 2)
    for book_id in (1, 10, 16, 20):  # the kept entry; years apart; another language; another series place
        with pytest.raises(ActionError):
            duplicates.hide_empty(book_id, client)
    assert client.excluded == [2]


def test_an_entry_that_got_a_file_meanwhile_is_not_hidden(bindery):
    with sqlite3.connect(bindery) as conn:
        conn.execute("INSERT INTO book_files(book_id, format, path) VALUES (2, 'audiobook', '/a/The Nightingale')")
    client = Client(bindery)

    with pytest.raises(ActionError):
        duplicates.hide_empty(2, client)
    assert client.excluded == []


def test_a_refusal_from_bindery_is_reported_and_recorded(bindery):
    class Refusing(Client):
        def exclude_book(self, book_id):
            return {"ok": False, "error": "book not found"}

    with pytest.raises(ActionError, match="book not found"):
        duplicates.hide_empty(2, Refusing(bindery))
    assert recent_cleanup_actions(1)[0]["status"] == "failed"


def test_a_split_is_joined_with_fix_match_then_the_emptied_entry_hidden(bindery):
    client = Client(bindery)

    message = duplicates.merge_split(3, client, poll_seconds=0)

    assert client.moved == [("/a/Client", 3)] and client.excluded == [4]
    assert "Joined" in message
    kinds = [a["action_kind"] for a in recent_cleanup_actions(2)]
    assert kinds == ["HIDE_DUPLICATE", "MERGE_DUPLICATE"]


def test_a_split_fix_match_cannot_do_changes_nothing(bindery):
    client = Client(bindery, preview="collision")

    with pytest.raises(ActionError, match="can't move"):
        duplicates.merge_split(3, client, poll_seconds=0)
    assert client.moved == [] and client.excluded == []


def test_fix_all_hides_and_joins_every_proven_pair_and_nothing_else(bindery):
    client = Client(bindery)

    done = duplicates.fix_all(client, auto=True, poll_seconds=0)

    assert done == {"hidden": 4, "merged": 1, "problems": []}
    assert sorted(client.excluded) == [2, 4, 8, 18, 24]
    action = recent_cleanup_actions(1)[0]
    assert action["followup"].startswith("Automatically")


def test_actions_off_changes_nothing(bindery, monkeypatch):
    monkeypatch.setattr(settings, "allow_actions", False)
    with pytest.raises(ActionError, match="Actions are disabled"):
        duplicates.fix_all(Client(bindery))


def test_the_hourly_run_needs_both_switches_and_waits_an_hour(bindery, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import scheduler

    started = []
    monkeypatch.setattr(duplicates, "start_fix", lambda auto=False: started.append(auto) or True)
    now = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(settings, "fix_duplicates", False)
    assert scheduler.run_duplicate_fix(now) is False
    monkeypatch.setattr(settings, "fix_duplicates", True)
    assert scheduler.run_duplicate_fix(now) is True
    assert scheduler.run_duplicate_fix(now + timedelta(minutes=30)) is False
    assert scheduler.run_duplicate_fix(now + timedelta(minutes=61)) is True
    assert started == [True, True]


def test_title_key_ignores_spelling_but_keeps_bracketed_words_and_every_alphabet():
    assert duplicates.title_key("The 117-Storey Treehouse") == duplicates.title_key("117 storey treehouse")
    assert duplicates.title_key("Get Well Soon, Mallory! (The Baby-Sitters Club #69)") == \
        duplicates.title_key("Get Well Soon Mallory (the Baby-Sitters Club #69)")
    assert duplicates.title_key("Hank (Book 1)") != duplicates.title_key("Hank (Book 2)")
    assert duplicates.title_key("战争 1") != duplicates.title_key("和平 1")
    assert duplicates.title_key("ถ้ำทะมึน") != duplicates.title_key("หน้าผาวิปโยค")


def _app():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routes.pages import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_duplicates_page_shows_what_is_kept_and_what_is_left_for_you(bindery):
    page = _app().get("/review/duplicates").text

    assert "data-duplicates-fix" in page and "Fixed automatically every hour" in page
    assert "will be hidden" in page and "kept" in page
    assert "Left for you: they are in different languages" in page


def test_fixes_read_plainly_in_activity(bindery):
    duplicates.hide_empty(2, Client(bindery))
    duplicates.fix_all(Client(bindery), auto=True, poll_seconds=0)
    client = _app()
    action_id = recent_cleanup_actions(1)[0]["id"]

    activity = client.get("/activity").text
    assert "You hid the empty extra Bindery entry" in activity
    assert "BookGuard joined" in activity and "BookGuard hid the empty extra Bindery entry" in activity
    assert client.get(f"/activity/cleanup/{action_id}").status_code == 200
