"""Bindery's unmatched files: junk, not in the library, or which book (with evidence)."""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from app import unmatched
from app.config import settings
from app.db import init_local_db

WORDS = " ".join(["the quiet house stood by the river and she walked home with her dog"] * 60)


def make_epub(path: Path, *, title: str = "", author: str = "", body: str = WORDS, extra: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = (f"<dc:title>{title}</dc:title>" if title else "") + (f"<dc:creator>{author}</dc:creator>" if author else "")
    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        epub.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"))
        epub.writestr("OEBPS/content.opf", (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">x</dc:identifier>{meta}'
            "<dc:language>en</dc:language></metadata>"
            '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>'))
        epub.writestr("OEBPS/c1.xhtml", (
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
            f"<h1>{title}</h1><p>{author}</p><p>{body}</p></body></html>"))
        for name, data in (extra or {}).items():
            epub.writestr(name, data)
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(settings, "ebook_root", str(tmp_path / "books"))
    monkeypatch.setattr(settings, "ebook_bindery_prefix", "/data/books")
    monkeypatch.setattr(settings, "audiobook_root", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(settings, "audiobook_bindery_prefix", "/data/audiobooks")
    monkeypatch.setattr(settings, "verification_malware_scan", False)
    monkeypatch.setattr(unmatched, "bindery_files_for_book", lambda book_id, fmt: [])
    init_local_db()
    library = unmatched.Library([
        {"id": 7236, "title": "Lights Out", "author": "James Patterson"},
        {"id": 12, "title": "Katt vs. Dogg", "author": "James Patterson"},
    ])
    return {"root": tmp_path / "books", "audio": tmp_path / "audiobooks", "library": library}


def row(rel: str, fmt: str = "ebook", **extra) -> dict:
    parts = rel.split("/")
    return {"id": 1, "kind": "file", "format": fmt, "rootPath": f"/data/{'audiobooks' if fmt == 'audiobook' else 'books'}",
            "relPath": rel, "authorFolder": parts[0] if len(parts) > 1 else "", "members": [parts[-1]], **extra}


def test_a_tiny_fake_ebook_is_junk(lib):
    (lib["root"] / "James Patterson/Die 6 Geisel").mkdir(parents=True)
    (lib["root"] / "James Patterson/Die 6 Geisel/Die 6 Geisel.epub").write_bytes(b"x" * 1008)

    outcome = unmatched.check_item(row("James Patterson/Die 6 Geisel/Die 6 Geisel.epub"), lib["library"], None)

    assert outcome["verdict"] == "JUNK"
    assert "1008 bytes" in outcome["reason"]


def test_a_real_ebook_proven_by_two_sources_belongs_to_its_book(lib):
    make_epub(lib["root"] / "James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub",
              title="Lights Out", author="James Patterson")

    outcome = unmatched.check_item(
        row("James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub"), lib["library"], None)

    assert (outcome["verdict"], outcome["bookId"]) == ("BELONGS", 7236)
    assert "Embedded metadata" in outcome["reason"] and "Title page" in outcome["reason"]


def test_a_real_book_by_an_author_not_in_the_library(lib):
    make_epub(lib["root"] / "Misc/Tycoon/Tycoon - Katy Evans.epub", title="Tycoon", author="Katy Evans")

    outcome = unmatched.check_item(row("Misc/Tycoon/Tycoon - Katy Evans.epub"), lib["library"], None)

    assert outcome["verdict"] == "NOT_IN_LIBRARY"
    assert "“Tycoon” by Katy Evans" in outcome["reason"]


def test_file_and_folder_names_alone_never_prove_a_book(lib):
    # No embedded title; the names and even the first page say "Lights Out" / "James Patterson".
    make_epub(lib["root"] / "James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub",
              body="Lights Out James Patterson " + WORDS)

    outcome = unmatched.check_item(
        row("James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub"), lib["library"], None)

    assert outcome["verdict"] == "UNSURE"


def test_a_sample_is_junk(lib):
    make_epub(lib["root"] / "A/B/Lights Out - James Patterson.epub", title="Lights Out", author="James Patterson",
              body=WORDS + " This is a free sample. Buy the full book to keep reading.")

    outcome = unmatched.check_item(row("A/B/Lights Out - James Patterson.epub"), lib["library"], None)

    assert outcome["verdict"] == "JUNK" and "sample" in outcome["reason"]


def test_a_drm_locked_ebook_is_junk(lib):
    make_epub(lib["root"] / "A/B/Locked.epub", title="Lights Out", author="James Patterson", extra={
        "META-INF/encryption.xml": '<encryption><EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/></encryption>'})

    outcome = unmatched.check_item(row("A/B/Locked.epub"), lib["library"], None)

    assert outcome["verdict"] == "JUNK"


def _audio_row(lib, monkeypatch, *, silent=False, tags=None):
    folder = lib["audio"] / "James Patterson/$10,000,000 Marriage Proposal"
    folder.mkdir(parents=True)
    names = [f"Katy Evans - Tycoon {n}-7.mp3" for n in range(1, 8)]
    for name in names:
        (folder / name).write_bytes(b"ID3")
    probe = {"audio_stream_count": 1, "duration_seconds": 1800, "album": "Tycoon", "artist": "Katy Evans", **(tags or {})}
    monkeypatch.setattr(unmatched, "cached_or_probe_audio_file", lambda path: ({"path": path, **probe}, False))
    monkeypatch.setattr(unmatched, "disc_track_sequence_warnings", lambda probes: [])
    monkeypatch.setattr(unmatched, "_silent", lambda path: silent)
    return {"id": 2, "kind": "folder", "format": "audiobook", "rootPath": "/data/audiobooks",
            "relPath": "James Patterson/$10,000,000 Marriage Proposal", "authorFolder": "James Patterson",
            "members": names}


def test_the_katy_evans_audiobook_in_a_patterson_folder_is_not_in_the_library(lib, monkeypatch):
    outcome = unmatched.check_item(_audio_row(lib, monkeypatch), lib["library"], None)

    assert outcome["verdict"] == "NOT_IN_LIBRARY"
    assert "“Tycoon” by Katy Evans" in outcome["reason"]
    folder = next(e for e in outcome["evidence"] if e["check"] == "Folder")
    assert folder["result"] == "warn" and "James Patterson" in folder["detail"]


def test_silent_audio_is_junk(lib, monkeypatch):
    outcome = unmatched.check_item(_audio_row(lib, monkeypatch, silent=True), lib["library"], None)

    assert outcome["verdict"] == "JUNK" and "silent" in outcome["reason"]


class FakeClient:
    def __init__(self, items):
        self.items, self.adopted = items, []

    def list_unmatched(self, *, search="", file_format=None, offset=0):
        page = self.items[offset:offset + 1]  # one per page proves paging
        return {"items": page, "total": len(self.items)}

    def lookup_isbn(self, isbn):
        return {}

    def adopt_unmatched(self, row_id, book_id):
        self.adopted.append((row_id, book_id))


def test_check_all_then_attach_only_a_proven_file(lib, monkeypatch):
    make_epub(lib["root"] / "James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub",
              title="Lights Out", author="James Patterson")
    (lib["root"] / "Junk").mkdir()
    (lib["root"] / "Junk/x.epub").write_bytes(b"x" * 10)
    client = FakeClient([
        {**row("James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub"), "id": 11},
        {**row("Junk/x.epub"), "id": 12},
    ])
    monkeypatch.setattr(unmatched, "library_books", lambda: list(lib["library"].books.values()))
    monkeypatch.setattr(unmatched, "BinderyClient", lambda **kwargs: client)
    monkeypatch.setattr(unmatched, "resolve_bindery_api_key", lambda: "key")
    monkeypatch.setattr(settings, "allow_actions", True)

    unmatched.check_unmatched(client)
    verdicts = {i["row_id"]: i["verdict"] for i in unmatched.stored_checks()}
    assert verdicts == {11: "BELONGS", 12: "JUNK"}

    with pytest.raises(unmatched.ActionError, match="Only files BookGuard proved"):
        unmatched.attach(12)
    assert "Lights Out" in unmatched.attach(11)
    assert client.adopted == [(11, 7236)]
    assert {i["row_id"] for i in unmatched.stored_checks()} == {12}


def test_unmatched_page_shows_each_verdict_with_its_reason(lib, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routes.pages import router

    unmatched.init_unmatched_db()
    item = {**row("A/B/x.epub"), "parsedTitle": "Die 6 Geisel"}
    unmatched._save(5, "sig", item, {"verdict": "JUNK", "reason": "The file is only 1008 bytes.",
                                     "evidence": [{"check": "Length", "result": "fail", "detail": "tiny"}]})
    unmatched._save(6, "sig", {**item, "parsedTitle": "Lights Out"},
                    {"verdict": "BELONGS", "reason": "It is “Lights Out”.", "bookId": 7236, "bookTitle": "Lights Out",
                     "evidence": []})
    monkeypatch.setattr(settings, "allow_actions", True)
    app = FastAPI()
    app.include_router(router)
    page = TestClient(app).get("/review/unmatched").text

    assert "Junk" in page and "The file is only 1008 bytes." in page
    assert 'data-unmatched-attach="6"' in page and "Attach to “Lights Out”" in page


def _cbz(path: Path, pages: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as cbz:
        for n in range(pages):
            cbz.writestr(f"page{n:03}.jpg", b"\xff\xd8\xff\xe0" + b"0" * 5000)
    return path


def test_a_comic_is_judged_by_its_pages_not_by_prose(lib):
    _cbz(lib["root"] / "A/Comic/Comic.cbz", 24)
    _cbz(lib["root"] / "A/Stub/Stub.cbz", 2)

    real = unmatched.check_item(row("A/Comic/Comic.cbz"), lib["library"], None)
    stub = unmatched.check_item(row("A/Stub/Stub.cbz"), lib["library"], None)

    assert real["verdict"] != "JUNK"
    assert any(e["detail"] == "24 pages of pictures (no text layer)." for e in real["evidence"])
    assert stub["verdict"] == "JUNK" and "only 2 pages" in stub["reason"]


def test_an_audiobook_folder_with_numbered_tracks_uses_its_folder_names(lib, monkeypatch):
    folder = lib["audio"] / "James Patterson/Lights Out"
    folder.mkdir(parents=True)
    for n in range(1, 4):
        (folder / f"{n:02}.mp3").write_bytes(b"ID3")
    probe = {"audio_stream_count": 1, "duration_seconds": 3600, "album": "Lights Out", "artist": "James Patterson"}
    monkeypatch.setattr(unmatched, "cached_or_probe_audio_file", lambda path: ({"path": path, **probe}, False))
    monkeypatch.setattr(unmatched, "disc_track_sequence_warnings", lambda probes: [])
    monkeypatch.setattr(unmatched, "_silent", lambda path: False)
    item = {"id": 3, "kind": "folder", "format": "audiobook", "rootPath": "/data/audiobooks",
            "relPath": "James Patterson/Lights Out", "authorFolder": "James Patterson",
            "members": ["01.mp3", "02.mp3", "03.mp3"]}

    outcome = unmatched.check_item(item, lib["library"], None)

    assert (outcome["verdict"], outcome["bookId"]) == ("BELONGS", 7236)
    assert "Audio tags + Folder names" in outcome["reason"]


def test_attach_rechecks_and_refuses_when_the_file_changed_or_is_gone(lib, monkeypatch):
    book = make_epub(lib["root"] / "James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub",
                     title="Lights Out", author="James Patterson")
    client = FakeClient([{**row("James Patterson/Lights Out (2015)/Lights Out - James Patterson.epub"), "id": 11}])
    monkeypatch.setattr(unmatched, "library_books", lambda: list(lib["library"].books.values()))
    monkeypatch.setattr(unmatched, "BinderyClient", lambda **kwargs: client)
    monkeypatch.setattr(unmatched, "resolve_bindery_api_key", lambda: "key")
    monkeypatch.setattr(settings, "allow_actions", True)
    unmatched.check_unmatched(client)

    book.write_bytes(b"x" * 100)  # replaced after the check
    with pytest.raises(unmatched.ActionError, match="changed since the last check"):
        unmatched.attach(11)
    assert client.adopted == []
    assert unmatched.stored_checks()[0]["verdict"] == "JUNK"  # the page shows the new result

    unmatched._save(11, unmatched._signature(client.items[0]), client.items[0],
                    {"verdict": "BELONGS", "reason": "", "bookId": 7236, "bookTitle": "Lights Out", "evidence": []})
    client.items.clear()  # Bindery no longer lists it
    with pytest.raises(unmatched.ActionError, match="no longer lists"):
        unmatched.attach(11)
    assert client.adopted == [] and unmatched.stored_checks() == []


def test_an_ebook_type_file_inside_the_audiobooks_folder_is_found_and_judged(lib):
    folder = lib["audio"] / "James Patterson/Die 6. Geisel ()"
    folder.mkdir(parents=True)
    (folder / "Die 6. Geisel.txt").write_bytes(b"x" * 1008)
    item = {"id": 60, "kind": "file", "format": "ebook", "rootPath": "/data/audiobooks",
            "relPath": "James Patterson/Die 6. Geisel ()/Die 6. Geisel.txt", "members": ["Die 6. Geisel.txt"]}

    outcome = unmatched.check_item(item, lib["library"], None)

    assert outcome["verdict"] == "JUNK" and "1008 bytes" in outcome["reason"]


def test_overlapping_library_prefixes_use_the_most_specific_one(lib, monkeypatch):
    monkeypatch.setattr(settings, "audiobook_bindery_prefix", "/data")
    monkeypatch.setattr(settings, "ebook_bindery_prefix", "/data/books")
    assert unmatched._local_path("/data/books/A/B.epub", "ebook") == str(lib["root"] / "A/B.epub")
    assert unmatched._local_path("/data/audio/A/B.mp3", "audiobook") == str(lib["audio"] / "audio/A/B.mp3")

    monkeypatch.setattr(settings, "audiobook_bindery_prefix", "/data/books")  # identical: the row's format decides
    assert unmatched._local_path("/data/books/A/B.epub", "ebook") == str(lib["root"] / "A/B.epub")
    assert unmatched._local_path("/data/books/A/B.mp3", "audiobook") == str(lib["audio"] / "A/B.mp3")


# ---- cases from a real library ---------------------------------------------------

@pytest.mark.parametrize("left, right", [
    ("05 - Nemesis Games", "Nemesis Games"),
    ("04 Cibola Burn", "Cibola Burn (Unabridged)"),
    ("Book 4 - Cibola Burn", "Cibola Burn: The Expanse, Book 4 (Unabridged)"),
    ("TDT 0.5 The Little Sisters of Eluria", "The Little Sisters of Eluria"),
    ("WMC 01 - 1st to Die", "1st to Die"),
    ("1993 - The Client", "The Client"),
    ("Maximum Ride 03 - Saving the World", "MR 3 - Saving the World"),
    ("James S. A. Corey - The Expanse - 2.0 - Caliban's War", "Calibans War"),
    ("Part 2 - Summer of Corruption - Apt Pupil", "Apt Pupil"),
    ("The Christmas Pig_UK", "Christmas Pig"),
    ("[Alex Cross 23] - Cross Justice", "Cross Justice"),
])
def test_titles_match_through_series_and_number_prefixes(left, right):
    assert unmatched._title_keys(left) & unmatched._title_keys(right)


@pytest.mark.parametrize("left, right", [
    ("1st to Die", "2nd Chance"), ("11/22/63", "Joyland"), ("Die 6. Geisel", "Geisel"), ("The Client", "The Firm"),
])
def test_different_titles_still_differ(left, right):
    assert not unmatched._title_keys(left) & unmatched._title_keys(right)


@pytest.mark.parametrize("name, surname", [
    ("J.K. Rowling", "rowling"), ("Rowling J.K.", "rowling"), ("Corey, James S.A.", "corey"),
    ("Patterson, James", "patterson"), ("James Patterson, Maxine Paetro", "patterson"),
    ("Author's", ""), ("unknown author", ""), ("05", ""), ("Martin Luther King Jr.", "king"),
])
def test_author_surnames_in_any_order(name, surname):
    assert unmatched._surname(name) == surname


def _audio(lib, monkeypatch, rel, tags, names=("01.mp3", "02.mp3")):
    folder = lib["audio"] / rel
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"ID3")
    probe = {"audio_stream_count": 1, "duration_seconds": 3600, **tags}
    monkeypatch.setattr(unmatched, "cached_or_probe_audio_file", lambda path: ({"path": path, **probe}, False))
    monkeypatch.setattr(unmatched, "_silent", lambda path: False)
    return {"id": 9, "kind": "folder", "format": "audiobook", "rootPath": "/data/audiobooks", "relPath": rel,
            "authorFolder": rel.split("/")[0], "members": list(names)}


def _library(*books):
    return unmatched.Library([{"id": n, "title": t, "author": a, **extra}
                              for n, (t, a, extra) in enumerate(books, start=1)])


def test_a_numbered_audiobook_belongs_to_its_book_not_outside_the_library(lib, monkeypatch):
    library = _library(("Nemesis Games", "James S. A. Corey", {}))
    item = _audio(lib, monkeypatch, "James S. A. Corey/Nemesis Games/Recovered - Gods of Risk folder/05 Nemesis Games",
                  {"album": "05 - Nemesis Games", "artist": "James S. A. Corey"})

    outcome = unmatched.check_item(item, library, None)

    assert (outcome["verdict"], outcome["bookId"]) == ("BELONGS", 1)


def test_tags_without_an_author_take_it_from_the_folder_and_generic_folders_are_skipped(lib, monkeypatch):
    library = _library(("The Client", "John Grisham", {}))
    item = _audio(lib, monkeypatch, "John Grisham/The Client (1993)/Edition 1",
                  {"album": "1993 - The Client", "artist": "Author's"})

    outcome = unmatched.check_item(item, library, None)

    assert (outcome["verdict"], outcome["bookId"]) == ("BELONGS", 1)
    assert "Audio tags + Folder names" in outcome["reason"]


def test_a_book_that_already_has_a_file_is_an_extra_copy_without_attach(lib, monkeypatch):
    library = _library(("Caliban's War", "James S. A. Corey", {}))
    monkeypatch.setattr(unmatched, "bindery_files_for_book", lambda book_id, fmt: [{"stored_path": "/elsewhere.m4b"}])
    item = _audio(lib, monkeypatch, "James S. A. Corey/Caliban's War/Book 2 - Calibans War",
                  {"album": "Caliban's War", "artist": "James S. A. Corey"})

    outcome = unmatched.check_item(item, library, None)

    assert outcome["verdict"] == "DUPLICATE" and "extra copy" in outcome["reason"]


def test_a_pen_name_for_the_folder_authors_book_is_unsure_not_outside_the_library(lib, monkeypatch):
    library = _library(("The Regulators", "Stephen King", {}))
    item = _audio(lib, monkeypatch, "Stephen King/The Regulators (1996)/The Regulators",
                  {"album": "The Regulators", "artist": "Richard Bachman"},
                  names=("Richard Bachman - The Regulators 01 of 67.mp3", "Richard Bachman - The Regulators 02 of 67.mp3"))

    outcome = unmatched.check_item(item, library, None)

    assert outcome["verdict"] == "UNSURE" and "pen name" in outcome["reason"]


def test_a_music_album_in_the_audiobooks_folder_is_junk(lib, monkeypatch):
    item = _audio(lib, monkeypatch, "Emily Giffin/Something Borrowed (2012)",
                  {"album": "Something Borrowed, Something New", "artist": "John Prine", "genre": "Country"})

    outcome = unmatched.check_item(item, _library(), None)

    assert outcome["verdict"] == "JUNK" and "music" in outcome["reason"]


def test_science_fiction_is_not_a_music_genre():
    assert not unmatched._music_genre("Science Fiction & Fantasy")
    assert not unmatched._music_genre("Audiobook")
    assert unmatched._music_genre("Folk/Country")


GERMAN = " ".join(["der mann und die frau ist nicht mit ein zu auf das haus ich sie"] * 60)


class IsbnClient(FakeClient):
    def lookup_isbn(self, isbn):
        return {"title": "Leviathan Wakes", "author": {"authorName": "James S. A. Corey"}}


def test_a_translation_is_another_language_edition_not_attach(lib, monkeypatch):
    monkeypatch.setattr(unmatched, "file_isbns", lambda identifiers: ["9783641076313"])
    library = _library(("Leviathan Wakes", "James S. A. Corey", {"language": "eng"}))
    make_epub(lib["root"] / "James S. A. Corey/Cibola brennt (2015)/eBook/Leviathan erwacht.epub",
              title="Expanse 01 - Leviathan erwacht", author="Corey, James S.A.",
              body="Leviathan Wakes James S. A. Corey " + GERMAN)

    outcome = unmatched.check_item(row("James S. A. Corey/Cibola brennt (2015)/eBook/Leviathan erwacht.epub"),
                                   library, IsbnClient([]))

    assert outcome["verdict"] == "OTHER_LANGUAGE" and "German edition" in outcome["reason"]

    unknown = _library(("Leviathan Wakes", "James S. A. Corey", {"language": ""}))  # Bindery doesn't say
    again = unmatched.check_item(row("James S. A. Corey/Cibola brennt (2015)/eBook/Leviathan erwacht.epub"),
                                 unknown, IsbnClient([]))
    assert again["verdict"] == "OTHER_LANGUAGE"


def test_author_name_order_does_not_hide_a_library_book(lib):
    library = _library(("Christmas Pig", "J. K. Rowling", {}))
    make_epub(lib["root"] / "J. K. Rowling/Christmas Pig ()/UK/The Christmas Pig_UK.epub",
              title="The Christmas Pig", author="Rowling J.K.")

    outcome = unmatched.check_item(row("J. K. Rowling/Christmas Pig ()/UK/The Christmas Pig_UK.epub"), library, None)

    assert (outcome["verdict"], outcome["bookId"]) == ("BELONGS", 1)
    assert not any(e["check"] == "Folder" for e in outcome["evidence"])


def test_files_numbered_part_of_total_are_not_missing_tracks():
    from app.audiobook_verification import disc_track_sequence_warnings

    def probes(numbers, total=35):
        return [{"path": f"/a/James S. A. Corey - Caliban's War {n:02}-{total}.mp3"} for n in numbers]

    assert disc_track_sequence_warnings(probes(range(1, 36))) == []
    assert disc_track_sequence_warnings(probes([1, 2, 4], total=10)) == ["Parts 3, 5, 6, 7, 8, 9, 10 of 10 are missing."]


def test_a_shortened_title_never_picks_between_two_books_of_one_author():
    library = _library(("Chronicles: First", "Ann Author", {}), ("Chronicles: Second", "Ann Author", {}),
                       ("Nemesis Games", "James S. A. Corey", {}))

    assert library.match("Chronicles: Second", "Ann Author")["id"] == 2  # the exact title wins
    assert library.match("Chronicles: First", "Ann Author")["id"] == 1
    assert library.match("Chronicles", "Ann Author") is None  # fits both: no guess
    assert library.match("05 - Nemesis Games", "James S. A. Corey")["id"] == 3  # one fit: fine


# ---- second real-library run ------------------------------------------------------

def test_a_book_listed_twice_in_the_library_is_unsure_and_says_so(lib, monkeypatch):
    library = _library(("A Time to Kill", "John Grisham", {}), ("Time to Kill", "John Grisham", {}))
    item = _audio(lib, monkeypatch, "John Grisham/A Time to Kill (1989)/1989 - A Time to Kill",
                  {"album": "1989 - A Time to Kill", "artist": "Author's"})

    outcome = unmatched.check_item(item, library, None)

    assert outcome["verdict"] == "UNSURE" and "more than once" in outcome["reason"]


def test_an_author_named_only_by_the_tags_against_the_folder_may_be_the_narrator(lib, monkeypatch):
    item = _audio(lib, monkeypatch, "Stephen King/Stephen King 8 ()/Der dunkle Turm 7 - Der Turm (uncut)",
                  {"album": "Der Turm (Der dunkle Turm 7)", "artist": "Vittorio Alfieri"},
                  names=("001 - Der Turm (Der dunkle Turm 7).mp3", "002 - Der Turm (Der dunkle Turm 7).mp3"))

    outcome = unmatched.check_item(item, _library(("The Dark Tower", "Stephen King", {})), None)

    assert outcome["verdict"] == "UNSURE" and "narrator" in outcome["reason"]


def test_double_dash_part_numbers_and_release_folders_still_name_the_book(lib, monkeypatch):
    item = _audio(lib, monkeypatch, "James Patterson/NYPD Red 3 (1755)/Peter David - Artful (u261~19-64.44-m-chap)",
                  {"album": "Artful", "artist": "Peter David"},
                  names=("Peter David - Artful 01--19.mp3", "Peter David - Artful 02--19.mp3"))

    outcome = unmatched.check_item(item, _library(), None)

    assert outcome["verdict"] == "NOT_IN_LIBRARY" and "“Artful” by Peter David" in outcome["reason"]


@pytest.mark.parametrize("rel, tags, title", [
    ("James S. A. Corey/Persepolis Rising (2017)/Primary edition",
     {"album": "Persepolis Rising (Unabridged)", "artist": "James S. A. Corey"}, "Persepolis Rising"),
    ("James Patterson/The Black Book (2017)/abook.ws~JsPn-TeBkBk-2017",
     {"album": "The Black Book (Unabridged)", "artist": "James Patterson, David Ellis"}, "The Black Book"),
    ("Cressida Cowell/Never and Forever (2020)/The Wizards of Once - Never and Forever (Book 4) - Cressida Cowell",
     {"album": "Never and Forever: The Wizards of Once, Book 4", "artist": "Cressida Cowell"}, "Never and Forever"),
    ("Stephen King and Joe Hill/In the Tall Grass (2012)/In the Tall Grass (Disc 01)",
     {"album": "In the Tall Grass 1", "artist": "Stephen King and Joe Hill"}, "In the Tall Grass"),
    ("James Patterson/The #1 Lawyer (2024)/Stingrays",
     {"album": "Stingrays", "artist": "James Patterson/Duane Swierczynski"}, "Stingrays"),
])
def test_more_real_folder_and_tag_shapes_belong_to_their_book(lib, monkeypatch, rel, tags, title):
    author = rel.split("/")[0]
    item = _audio(lib, monkeypatch, rel, tags)

    outcome = unmatched.check_item(item, _library((title, author, {})), None)

    assert (outcome["verdict"], outcome.get("bookId")) == ("BELONGS", 1), outcome["reason"]


def test_a_series_name_and_number_is_not_a_book_title(lib, monkeypatch):
    item = _audio(lib, monkeypatch, "Martha Wells/Murderbot Diaries ()/Murderbot Diaries 05",
                  {"album": "Murderbot Diaries 05", "artist": "Martha Wells"})

    outcome = unmatched.check_item(item, _library(("Network Effect", "Martha Wells", {})), None)

    assert outcome["verdict"] == "UNSURE" and "series name and number" in outcome["reason"]


@pytest.mark.parametrize("genre, music", [
    ("Progressive Rock", True), ("Classic Country", True), ("Folk Tales", False), ("Pop Culture History", False),
    ("Young Adult/Fantasy", False), ("Speech", False),
])
def test_music_genres_by_their_words(genre, music):
    assert unmatched._music_genre(genre) is music


def test_books_excluded_in_bindery_are_not_in_the_library(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "bindery.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER, language TEXT,
                                excluded INTEGER NOT NULL DEFAULT 0);
            INSERT INTO authors VALUES (1, 'John Grisham');
            INSERT INTO books VALUES (1, 'A Time to Kill', 1, 'eng', 1), (2, 'The Firm', 1, 'eng', 0);
        """)
    monkeypatch.setattr(settings, "bindery_db", str(db))

    assert [b["title"] for b in unmatched.library_books()] == ["The Firm"]
