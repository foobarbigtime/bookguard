from __future__ import annotations

import json
import os
import sqlite3
import zipfile

import pytest

from app.actions import ActionError
import app.hardlink_conflicts as hardlinks


class FakeClient:
    def __init__(self, db):
        self.db = db
        self.calls = []
        self.queue = {"items": [], "partial": False}
        self.settings = {"autoGrab.enabled": "false", "import.mode": "external"}
        self.after_detach = None
        self.error = None

    def get_setting(self, key):
        return self.settings[key]

    def list_queue(self):
        return self.queue

    def get_book(self, book_id):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            book = conn.execute("SELECT id,title FROM books WHERE id=?", (book_id,)).fetchone()
            files = [dict(row) for row in conn.execute(
                "SELECT id,book_id AS bookId,format,path FROM book_files WHERE book_id=?",
                (book_id,),
            )]
        ebooks = [f["path"] for f in files if f["format"] == "ebook"]
        audio = [f["path"] for f in files if f["format"] == "audiobook"]
        return {**dict(book), "author": {"name": "Daniel Silva"},
                "ebookFilePath": ebooks[0] if ebooks else "",
                "audiobookFilePath": audio[0] if audio else "",
                "mediaType": "both" if ebooks and audio else "audiobook",
                "bookFiles": files}

    def deregister_file(self, book_id, path):
        self.calls.append((book_id, path))
        if self.error:
            raise self.error
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM book_files WHERE book_id=? AND path=?", (book_id, path))
        if self.after_detach:
            self.after_detach()


def write_epub(path, text="English Girl Daniel Silva " + "Story text. " * 100):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml",
                         '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
        archive.writestr("content.opf", '''<package xmlns:dc="http://purl.org/dc/elements/1.1/">
            <metadata><dc:title>English Girl</dc:title><dc:creator>Daniel Silva</dc:creator></metadata>
            <manifest><item id="title" href="title.xhtml" media-type="application/xhtml+xml"/></manifest>
            <spine><itemref idref="title"/></spine></package>''')
        archive.writestr("title.xhtml", f"<html><body>{text}</body></html>")


@pytest.fixture
def setup_conflict(tmp_path, monkeypatch):
    root = tmp_path / "books"
    root.mkdir()
    final = root / "English Girl.epub"
    write_epub(final)
    stage = root / ".bindery-stage-123-English Girl.epub"
    os.link(final, stage)
    db = tmp_path / "bindery.db"
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE authors(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,status TEXT,author_id INTEGER);
            CREATE TABLE book_files(id INTEGER PRIMARY KEY,book_id INTEGER,format TEXT,path TEXT);
            INSERT INTO authors VALUES(67,'Daniel Silva');
            INSERT INTO books VALUES(6132,'English Girl','imported',67);
            INSERT INTO books VALUES(6495,'English Assassin','imported',67);
        ''')
        conn.executemany("INSERT INTO book_files VALUES(?,?,?,?)", [
            (3283, 6132, "ebook", "/data/books/English Girl.epub"),
            (3284, 6495, "ebook", "/data/books/.bindery-stage-123-English Girl.epub"),
            (1149, 6495, "audiobook", "/data/audio/English Assassin"),
            (1151, 6132, "audiobook", "/data/audio/English Girl"),
        ])
    for key, value in {"ebook_root": str(root), "ebook_bindery_prefix": "/data/books",
                       "bindery_db": str(db), "config_dir": str(tmp_path / "config"),
                       "allow_actions": False, "verification_use_tika": False,
                       **{key: True for key in hardlinks._POLICY}}.items():
        monkeypatch.setattr(hardlinks.settings, key, value)
    return FakeClient(db), final, stage


def test_detects_cross_book_hardlink_but_preview_is_audit_only(setup_conflict):
    client, _, _ = setup_conflict
    report = hardlinks.hardlink_conflicts()
    assert report["items"][0]["candidateFileId"] == 3284
    preview = hardlinks.conflict_preview(3284, client)
    assert preview["safe"] is True
    assert preview["ready"] is False
    assert preview["retained"]["book_id"] == 6132
    assert client.calls == []


def test_actions_disabled_cannot_mutate(setup_conflict):
    client, _, _ = setup_conflict
    proof = hardlinks.conflict_preview(3284, client)
    with pytest.raises(ActionError, match="disabled"):
        hardlinks.correct_hardlink_conflict(3284, proof["token"], client)
    assert client.calls == []


def test_corrects_only_wrong_ebook_preserves_hardlinks_and_audiobooks(setup_conflict, monkeypatch):
    client, final, stage = setup_conflict
    proof = hardlinks.conflict_preview(3284, client)
    original = final.read_bytes()
    monkeypatch.setattr(hardlinks.settings, "allow_actions", True)
    result = hardlinks.correct_hardlink_conflict(3284, proof["token"], client)
    assert result["ok"] and not result["filesChanged"]
    assert client.calls == [(6495, "/data/books/.bindery-stage-123-English Girl.epub")]
    assert final.read_bytes() == stage.read_bytes() == original
    assert final.stat().st_ino == stage.stat().st_ino
    assert final.stat().st_nlink == 2
    assert client.get_book(6132)["ebookFilePath"] == "/data/books/English Girl.epub"
    assert client.get_book(6495)["ebookFilePath"] == ""
    assert client.get_book(6495)["audiobookFilePath"] == "/data/audio/English Assassin"
    assert client.get_book(6132)["audiobookFilePath"] == "/data/audio/English Girl"
    assert hardlinks.correction_history()[0]["status"] == "applied"


def test_identical_copies_are_not_hardlink_conflicts(setup_conflict):
    client, final, stage = setup_conflict
    stage.unlink()
    stage.write_bytes(final.read_bytes())
    assert hardlinks.hardlink_conflicts()["items"] == []
    assert hardlinks.conflict_preview(3284, client)["safe"] is False


@pytest.mark.parametrize("change", ["third_link", "symlink", "unsafe_epub", "ambiguous_identity"])
def test_blocks_unsafe_or_ambiguous_files(setup_conflict, change):
    client, final, stage = setup_conflict
    if change == "third_link":
        os.link(final, final.parent / "third.epub")
    elif change == "symlink":
        stage.unlink()
        stage.symlink_to(final)
    elif change == "unsafe_epub":
        final.write_bytes(b"broken")
    else:
        write_epub(final, "English Assassin Daniel Silva English Girl " + "Story. " * 100)
    assert hardlinks.conflict_preview(3284, client)["safe"] is False
    assert client.calls == []


@pytest.mark.parametrize("queue", [
    {"items": [], "partial": True},
    {"items": []},
    {"items": [{"status": "importexternal"}], "partial": False},
    {"items": [{}], "partial": False},
])
def test_blocks_partial_or_active_queue(setup_conflict, queue):
    client, _, _ = setup_conflict
    client.queue = queue
    assert hardlinks.conflict_preview(3284, client)["safe"] is False
    assert client.calls == []


@pytest.mark.parametrize("key,value", [("autoGrab.enabled", "true"), ("import.mode", "auto")])
def test_blocks_unsafe_bindery_settings(setup_conflict, key, value):
    client, _, _ = setup_conflict
    client.settings[key] = value
    assert hardlinks.conflict_preview(3284, client)["safe"] is False


def test_blocks_disabled_integrity_gate(setup_conflict, monkeypatch):
    client, _, _ = setup_conflict
    monkeypatch.setattr(hardlinks.settings, "verification_archive_safety", False)
    assert hardlinks.conflict_preview(3284, client)["safe"] is False


def test_stale_preview_cannot_detach(setup_conflict, monkeypatch):
    client, final, _ = setup_conflict
    proof = hardlinks.conflict_preview(3284, client)
    write_epub(final, "English Girl Daniel Silva " + "Changed story. " * 100)
    monkeypatch.setattr(hardlinks.settings, "allow_actions", True)
    with pytest.raises(ActionError, match="preview changed"):
        hardlinks.correct_hardlink_conflict(3284, proof["token"], client)
    assert client.calls == []


def test_checks_api_database_agreement(setup_conflict):
    client, _, _ = setup_conflict
    original = client.get_book
    client.get_book = lambda book_id: {**original(book_id), "title": "Changed title"}
    assert hardlinks.conflict_preview(3284, client)["safe"] is False


@pytest.mark.parametrize("author_fields", [
    {"author": {"id": 67, "authorName": "Daniel Silva"}},
    {"author": {"name": "Daniel Silva"}},
    {"authorName": "Daniel Silva", "author": None},
])
def test_author_api_shapes_preserve_exact_identity_check(setup_conflict, author_fields, monkeypatch):
    client, _, _ = setup_conflict
    original = client.get_book
    client.get_book = lambda book_id: {**original(book_id), "authorId": 67, **author_fields}
    proof = hardlinks.conflict_preview(3284, client)
    assert proof["safe"] is True
    assert proof["books"]["6495"]["authorName"] == "Daniel Silva"
    monkeypatch.setattr(hardlinks.settings, "allow_actions", True)
    assert hardlinks.correct_hardlink_conflict(3284, proof["token"], client)["ok"] is True


@pytest.mark.parametrize("author_fields", [
    {"author": {"authorName": "Someone Else"}},
    {"author": None},
    {"authorName": "Someone Else", "author": {"authorName": "Daniel Silva"}},
])
def test_wrong_or_missing_api_author_remains_blocked(setup_conflict, author_fields):
    client, _, _ = setup_conflict
    original = client.get_book
    client.get_book = lambda book_id: {**original(book_id), **author_fields}
    assert hardlinks.conflict_preview(3284, client)["safe"] is False
    assert client.calls == []


def test_uncertain_response_is_durable_and_blocks_retry(setup_conflict, monkeypatch):
    client, _, _ = setup_conflict
    proof = hardlinks.conflict_preview(3284, client)
    client.error = RuntimeError("connection lost")
    monkeypatch.setattr(hardlinks.settings, "allow_actions", True)
    with pytest.raises(ActionError, match="connection lost"):
        hardlinks.correct_hardlink_conflict(3284, proof["token"], client)
    assert hardlinks.correction_history()[0]["status"] == "needs_review"
    with pytest.raises(ActionError, match="uncertain"):
        hardlinks.correct_hardlink_conflict(3284, proof["token"], client)
    assert len(client.calls) == 1


def test_audiobook_change_after_mutation_is_not_success(setup_conflict, monkeypatch):
    client, _, _ = setup_conflict
    proof = hardlinks.conflict_preview(3284, client)
    def change_audio():
        with sqlite3.connect(client.db) as conn:
            conn.execute("DELETE FROM book_files WHERE id=1149")
    client.after_detach = change_audio
    monkeypatch.setattr(hardlinks.settings, "allow_actions", True)
    with pytest.raises(ActionError, match="postconditions"):
        hardlinks.correct_hardlink_conflict(3284, proof["token"], client)
    assert hardlinks.correction_history()[0]["status"] == "needs_review"
    with hardlinks.local_conn() as conn:
        snapshot = json.loads(conn.execute("SELECT snapshot_json FROM hardlink_corrections").fetchone()[0])
    assert snapshot["books"]["6495"]["audiobookFilePath"] == "/data/audio/English Assassin"


def test_outside_prefix_and_parent_traversal_are_blocked(setup_conflict):
    _, _, _ = setup_conflict
    for value in ("/other/books/file.epub", "/data/books/../escape.epub"):
        with pytest.raises(ActionError):
            hardlinks._local_path({"stored_path": value})


def test_reconcile_uncertain_no_change_cancels_without_repeating_mutation(setup_conflict, monkeypatch):
    client, _, _ = setup_conflict
    proof = hardlinks.conflict_preview(3284, client)
    client.error = RuntimeError("connection lost")
    monkeypatch.setattr(hardlinks.settings, "allow_actions", True)
    with pytest.raises(ActionError):
        hardlinks.correct_hardlink_conflict(3284, proof["token"], client)
    entry = hardlinks.correction_history()[0]
    assert hardlinks.reconcile_correction(entry["id"], client)["status"] == "cancelled"
    assert len(client.calls) == 1
    assert hardlinks.conflict_preview(3284, client)["safe"] is True


def test_reconcile_lost_response_adopts_completed_change_without_retry(setup_conflict, monkeypatch):
    client, _, _ = setup_conflict
    proof = hardlinks.conflict_preview(3284, client)
    def lose_response():
        raise RuntimeError("response lost after commit")
    client.after_detach = lose_response
    monkeypatch.setattr(hardlinks.settings, "allow_actions", True)
    with pytest.raises(ActionError):
        hardlinks.correct_hardlink_conflict(3284, proof["token"], client)
    entry = hardlinks.correction_history()[0]
    assert hardlinks.reconcile_correction(entry["id"], client)["status"] == "applied"
    assert len(client.calls) == 1


def test_route_requires_exact_confirmation_and_defaults_block_mutation(setup_conflict, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes.hardlink_conflicts import router
    client, _, _ = setup_conflict
    monkeypatch.setattr(hardlinks, "BinderyClient", lambda: client)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as api:
        proof = api.get("/api/hardlink-conflicts/3284/preview").json()
        assert proof["safe"] is True
        assert api.post("/api/hardlink-conflicts/3284/correct", json={
            "confirm": "remove_wrong_ebook_association", "token": proof["token"],
        }).status_code == 400
        assert api.post("/api/hardlink-conflicts/3284/correct", json={
            "confirm": "REMOVE_WRONG_EBOOK_ASSOCIATION", "token": proof["token"],
        }).status_code == 409
        assert api.post("/api/hardlink-conflicts/3284/correct", json={
            "confirm": "REMOVE_WRONG_EBOOK_ASSOCIATION", "token": "bad",
        }).status_code == 422
    assert client.calls == []
