"""Books Bindery lists twice: found, sorted by kind, and only empty extras hidden."""

from __future__ import annotations

import sqlite3

import pytest

from app import duplicates
from app.actions import ActionError
from app.config import settings
from app.db import init_local_db, recent_cleanup_actions

BOOKS = [
    # id, author, title, year, ebook, audiobook, excluded
    (1, 1, "Nightingale", "2015-02-03", "/b/Nightingale.epub", "", 0),
    (2, 1, "The Nightingale", "2015", "", "", 0),
    (3, 2, "The Client", "1993", "/b/The Client.epub", "", 0),
    (4, 2, "Client", "", "", "/a/Client", 0),
    (5, 2, "Time to Kill", "", "/b/Time to Kill.mobi", "", 0),
    (6, 2, "A Time to Kill", "1989", "/b/A Time to Kill.epub", "", 0),
    (7, 3, "Superman / Shazam!", "", "", "", 0),
    (8, 3, "Superman/Shazam!", "", "", "", 0),
    (9, 4, "Lost", "2020", "/b/Lost.epub", "", 0),
    (10, 4, "The Lost", "2014", "", "", 0),
    (11, 5, "Hank (Book 1)", "", "/b/Hank1.epub", "", 0),
    (12, 5, "Hank (Book 2)", "", "", "", 0),
    (13, 6, "หน้าผาวิปโยค", "", "", "", 0),
    (14, 6, "ถ้ำทะมึน", "", "", "", 0),
    (15, 7, "Order", "", "/b/Order.epub", "", 0),
    (16, 7, "The Order", "", "", "", 1),  # already excluded: Bindery doesn't show it
]


@pytest.fixture
def bindery(tmp_path, monkeypatch):
    db = tmp_path / "bindery.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT, release_date TEXT,
                ebook_file_path TEXT NOT NULL DEFAULT '', audiobook_file_path TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '', excluded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT, path TEXT);
        """)
        conn.executemany("INSERT INTO authors VALUES (?, ?)", [
            (1, "Kristin Hannah"), (2, "John Grisham"), (3, "Jeff Parker"), (4, "James Patterson"),
            (5, "Steven Campbell"), (6, "Lemony Snicket"), (7, "Daniel Silva")])
        conn.executemany("INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, '', ?)", BOOKS)
    monkeypatch.setattr(settings, "bindery_db", str(db))
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "allow_actions", True)
    init_local_db()
    return db


def _by_title(found):
    return {g["entries"][0]["title"]: g for g in found}


def test_books_listed_twice_are_found_and_sorted_by_kind(bindery):
    found = _by_title(duplicates.find_duplicates())

    assert set(found) == {"Nightingale", "The Client", "Time to Kill", "Superman / Shazam!", "Lost"}
    assert (found["Nightingale"]["kind"], found["Nightingale"]["hideable"]) == ("EMPTY_EXTRA", [2])
    assert found["The Client"]["kind"] == "SPLIT"
    assert found["Time to Kill"]["kind"] == "TWO_COPIES"
    assert found["Superman / Shazam!"]["kind"] == "ALL_EMPTY"
    assert found["Lost"]["hideable"] == []  # 2020 and 2014: maybe two books


def test_hiding_excludes_only_the_empty_entry_and_records_it(bindery):
    calls = []

    class Client:
        def toggle_book_excluded(self, book_id):
            calls.append(book_id)
            return {"id": book_id, "excluded": True}

    message = duplicates.hide_empty(2, Client())

    assert calls == [2] and "The Nightingale" in message
    action = recent_cleanup_actions(1)[0]
    assert (action["action_kind"], action["status"], action["book_id"]) == ("HIDE_DUPLICATE", "applied", 2)
    with pytest.raises(ActionError, match="can't be hidden safely"):
        duplicates.hide_empty(1, Client())  # the entry with the files
    with pytest.raises(ActionError, match="can't be hidden safely"):
        duplicates.hide_empty(10, Client())  # different years
    assert calls == [2]


def test_an_entry_that_got_a_file_meanwhile_is_not_hidden(bindery):
    with sqlite3.connect(bindery) as conn:
        conn.execute("INSERT INTO book_files(book_id, format, path) VALUES (2, 'audiobook', '/a/The Nightingale')")

    class Client:
        def toggle_book_excluded(self, book_id):
            raise AssertionError("must not be called")

    with pytest.raises(ActionError):
        duplicates.hide_empty(2, Client())


def test_an_entry_hidden_elsewhere_meanwhile_stays_hidden(bindery):
    states = iter([False, True])  # the first toggle found it already hidden and showed it again

    class Client:
        calls = 0

        def toggle_book_excluded(self, book_id):
            Client.calls += 1
            return {"excluded": next(states)}

    with pytest.raises(ActionError, match="already hidden"):
        duplicates.hide_empty(2, Client())
    assert Client.calls == 2


def test_hide_all_hides_every_safe_empty_extra(bindery):
    hidden = []

    class Client:
        def toggle_book_excluded(self, book_id):
            hidden.append(book_id)
            with sqlite3.connect(bindery) as conn:
                conn.execute("UPDATE books SET excluded = 1 WHERE id = ?", (book_id,))
            return {"excluded": True}

    assert duplicates.hide_all_empty(Client()) == {"hidden": 1, "problems": []}
    assert hidden == [2]


def test_actions_off_changes_nothing(bindery, monkeypatch):
    monkeypatch.setattr(settings, "allow_actions", False)
    with pytest.raises(ActionError, match="Actions are disabled"):
        duplicates.hide_empty(2)


def test_title_key_ignores_spelling_but_keeps_bracketed_words():
    assert duplicates.title_key("The 117-Storey Treehouse") == duplicates.title_key("117 storey treehouse")
    assert duplicates.title_key("Get Well Soon, Mallory! (The Baby-Sitters Club #69)") == \
        duplicates.title_key("Get Well Soon Mallory (the Baby-Sitters Club #69)")
    assert duplicates.title_key("Hank (Book 1)") != duplicates.title_key("Hank (Book 2)")
    assert duplicates.title_key("ถ้ำทะมึน") == ""


def test_duplicates_page_lists_groups_and_offers_hiding(bindery):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routes.pages import router

    app = FastAPI()
    app.include_router(router)
    page = TestClient(app).get("/review/duplicates").text

    assert "An extra entry with no files" in page and 'data-duplicate-hide="2"' in page
    assert "Hide 1 empty extra entries" in page
    assert 'data-duplicate-hide="10"' not in page and "different years" in page


def test_a_hidden_duplicate_reads_plainly_in_activity(bindery):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routes.pages import router

    class Client:
        def toggle_book_excluded(self, book_id):
            return {"excluded": True}

    duplicates.hide_empty(2, Client())
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    action_id = recent_cleanup_actions(1)[0]["id"]

    assert "You hid the empty extra Bindery entry" in client.get("/activity").text
    detail = client.get(f"/activity/cleanup/{action_id}")
    assert detail.status_code == 200 and "The Nightingale" in detail.text
