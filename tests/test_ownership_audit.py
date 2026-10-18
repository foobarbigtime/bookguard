"""Regression tests for read-only Bindery ownership auditing."""

import sqlite3
import unicodedata

import pytest

from app.config import settings
from app.ownership_audit import _ancestors, audit_ownership


@pytest.fixture
def ownership_db(tmp_path, monkeypatch):
    path = tmp_path / "bindery.db"

    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                title TEXT
            );
            CREATE TABLE book_files (
                id INTEGER PRIMARY KEY,
                book_id INTEGER,
                path TEXT
            );
        """)

        conn.executemany(
            "INSERT INTO books VALUES (?, ?)",
            [
                (10, "Collected Audiobooks"),
                (20, "Other Book"),
                (30, "Unrelated Book"),
            ],
        )

        conn.executemany(
            "INSERT INTO book_files VALUES (?, ?, ?)",
            [
                (1, 10, "/audiobooks/Collected"),
                (2, 20, "/audiobooks/Collected/Disc 1/Track.mp3"),
                (3, 30, "/audiobooks/Collected 2/Track.mp3"),
                (4, 10, "/audiobooks/Collected/Disc 1/Other.mp3"),
            ],
        )

    monkeypatch.setattr(settings, "bindery_db", str(path))
    return path


def test_detects_nested_cross_book_ownership(ownership_db):
    conflicts = audit_ownership()

    assert len(conflicts) == 1
    assert conflicts[0]["parent_book_id"] == 10
    assert conflicts[0]["child_book_id"] == 20
    assert conflicts[0]["child_file_id"] == 2


def test_ignores_sibling_and_same_book_paths(ownership_db):
    conflicts = audit_ownership()

    assert all(row["child_file_id"] != 3 for row in conflicts)
    assert all(row["child_file_id"] != 4 for row in conflicts)


@pytest.mark.parametrize(
    "path",
    [
        "relative/Track.mp3",
        "/audiobooks/../Track.mp3",
    ],
)
def test_rejects_invalid_paths(path):
    with pytest.raises(ValueError):
        _ancestors(path)


def test_missing_database_fails_instead_of_reporting_zero(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        settings, "bindery_db", str(tmp_path / "missing.db")
    )

    with pytest.raises((sqlite3.Error, OSError)):
        audit_ownership()


def test_unicode_normalization_conflict(ownership_db):
    parent = unicodedata.normalize("NFD", "/audiobooks/Café")
    child = unicodedata.normalize("NFC", "/audiobooks/Café/Track.mp3")

    with sqlite3.connect(ownership_db) as conn:
        conn.execute(
            "INSERT INTO book_files VALUES (?, ?, ?)",
            (5, 10, parent),
        )
        conn.execute(
            "INSERT INTO book_files VALUES (?, ?, ?)",
            (6, 20, child),
        )

    conflicts = audit_ownership()

    assert any(row["child_file_id"] == 6 for row in conflicts)


@pytest.mark.parametrize(
    "path",
    [
        "/audiobooks//Collected/Track.mp3",
        "/audiobooks/./Collected/Track.mp3",
        "//audiobooks/Collected/Track.mp3",
    ],
)
def test_noncanonical_paths_are_rejected(path):
    with pytest.raises(ValueError):
        _ancestors(path)


def test_malformed_parent_record_fails_audit(ownership_db):
    """A malformed parent must never be silently ignored."""
    with sqlite3.connect(ownership_db) as conn:
        conn.execute(
            "UPDATE book_files SET path = ? WHERE id = 1",
            ("/audiobooks//Collected",),
        )

    with pytest.raises(ValueError, match="Invalid Bindery path"):
        audit_ownership()


def test_legacy_trailing_separator_is_accepted():
    from app.ownership_audit import _ancestors

    assert _ancestors("/audiobooks/Book/") == ("/audiobooks",)


def test_multiple_trailing_separators_are_rejected():
    from app.ownership_audit import _ancestors

    with pytest.raises(ValueError):
        _ancestors("/audiobooks/Book//")


def test_legacy_parent_trailing_separator_detects_conflict(ownership_db):
    """Legacy directory paths retain their original spelling in results."""
    with sqlite3.connect(ownership_db) as conn:
        conn.execute(
            "UPDATE book_files SET path = ? WHERE id = 1",
            ("/audiobooks/Collected/",),
        )

    conflicts = audit_ownership()

    assert len(conflicts) == 1
    assert conflicts[0]["parent_file_id"] == 1
    assert conflicts[0]["parent_book_id"] == 10
    assert conflicts[0]["parent_path"] == "/audiobooks/Collected/"
    assert conflicts[0]["child_file_id"] == 2
    assert conflicts[0]["child_book_id"] == 20
    assert conflicts[0]["child_path"] == (
        "/audiobooks/Collected/Disc 1/Track.mp3"
    )
