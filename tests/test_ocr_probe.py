"""OCR of image-only PDFs: bounded, optional, and only ever able to make a match fail, not invent one."""

from __future__ import annotations

import shutil

import pytest

from app import ebook_extraction, ocr_probe
from app.config import settings
from app.verification_engine import classify_identity
from app.verifier import predates_ocr

NEEDS_TOOLS = pytest.mark.skipif(
    not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="Tesseract and Poppler are not installed")


def title_page_pdf(path, lines, *, metadata=None):
    """An image-only PDF: the text is pixels, with no text layer (what a scan looks like)."""
    pil = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    font_module = pytest.importorskip("PIL.ImageFont")
    page = pil.new("L", (1275, 1650), 255)
    draw = draw_module.Draw(page)
    font = font_module.load_default(size=64)
    y = 420
    for line in lines:
        draw.text((120, y), line, fill=0, font=font)
        y += 110
    page.save(path, "PDF", resolution=150, **(metadata or {}))
    return path


@NEEDS_TOOLS
def test_the_title_page_of_an_image_only_pdf_is_read(tmp_path):
    pdf = title_page_pdf(tmp_path / "scan.pdf", ["HOW TO STEAL A", "DRAGON'S SWORD", "CRESSIDA COWELL"])
    found = ocr_probe.ocr_pdf_front(pdf)
    assert "error" not in found, found
    text = found["text"].lower()
    assert "dragon" in text and "cowell" in text and found["pages"] >= 1


@NEEDS_TOOLS
def test_an_image_only_pdf_with_placeholder_metadata_is_now_recognised_as_the_right_book(tmp_path):
    pdf = title_page_pdf(tmp_path / "scan.pdf", ["HOW TO STEAL A", "DRAGON'S SWORD", "CRESSIDA COWELL"],
                         metadata={"title": "Book to Do 1", "author": "Unknown"})
    extracted = ebook_extraction.extract_ebook_identity(str(pdf))
    assert extracted.source == "native-pdf+ocr" and extracted.ocr and extracted.ocr["chars"] > 20
    assert any("OCR" in note for note in extracted.notes)
    result = {"title": "How to Steal a Dragon's Sword", "author": "Cressida Cowell", "book_id": 1,
              "series": [], "isbn_evidence": {}, "_ocr_text": extracted.source.endswith("+ocr")}
    verdict, _confidence, evidence = classify_identity(
        result, extracted.metadata, extracted.text, extracted.identifiers, extracted.notes, extracted.front_text)
    # The page itself names the assigned book while the file's own details are junk.
    assert verdict in {"METADATA_ERROR", "VERIFIED_CORRECT"}, (verdict, evidence.get("explanation"))
    assert evidence["content"]["expected_signal"]["title_found"] is True


def _classify_scan(path, title, author):
    extracted = ebook_extraction.extract_ebook_identity(str(path))
    assert extracted.source == "native-pdf+ocr", extracted.notes
    result = {"title": title, "author": author, "book_id": 1, "series": [], "isbn_evidence": {},
              "_ocr_text": True}
    return classify_identity(
        result, extracted.metadata, extracted.text, extracted.identifiers, extracted.notes, extracted.front_text)


@NEEDS_TOOLS
def test_a_scan_of_a_different_book_does_not_verify_when_its_metadata_copies_the_catalogue(tmp_path):
    """The failure a reviewer reproduced end to end: wrong page, right-looking metadata."""
    copied = {"title": "The Last Colony", "author": "John Scalzi"}
    for lines in (["THE LOST COLONY", "EOIN COLFER"], ["THE LOST COLONY", "JOHN SCALZI"],
                  ["ZOE'S TALE", "JOHN SCALZI"]):
        pdf = title_page_pdf(tmp_path / "scan.pdf", lines, metadata=copied)
        verdict, confidence, evidence = _classify_scan(pdf, "The Last Colony", "John Scalzi")
        assert verdict not in {"VERIFIED_CORRECT", "METADATA_ERROR"}, (lines, verdict, confidence)


@NEEDS_TOOLS
def test_a_scan_of_the_right_book_still_verifies_when_its_metadata_copies_the_catalogue(tmp_path):
    pdf = title_page_pdf(tmp_path / "scan.pdf", ["THE LAST COLONY", "JOHN SCALZI"],
                         metadata={"title": "The Last Colony", "author": "John Scalzi"})
    verdict, confidence, _ = _classify_scan(pdf, "The Last Colony", "John Scalzi")
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 99)


def test_ocr_is_skipped_when_disabled_or_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "verification_ocr", False)
    assert ocr_probe.ocr_pdf_front(tmp_path / "x.pdf") == {"disabled": True}
    monkeypatch.setattr(settings, "verification_ocr", True)
    monkeypatch.setattr(ocr_probe.shutil, "which", lambda name: None)
    assert "not available" in ocr_probe.ocr_pdf_front(tmp_path / "x.pdf")["error"]


@NEEDS_TOOLS
def test_a_file_that_is_not_a_pdf_or_is_damaged_is_refused_without_running_anything(tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf at all")
    assert "error" in ocr_probe.ocr_pdf_front(fake)
    truncated = tmp_path / "trunc.pdf"
    truncated.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\n")          # no %%EOF
    assert "error" in ocr_probe.ocr_pdf_front(truncated)


@NEEDS_TOOLS
def test_a_blank_scan_reports_no_text_instead_of_inventing_any(tmp_path):
    pil = pytest.importorskip("PIL.Image")
    pil.new("L", (800, 1000), 255).save(tmp_path / "blank.pdf", "PDF", resolution=150)
    found = ocr_probe.ocr_pdf_front(tmp_path / "blank.pdf")
    assert "text" not in found and "no text" in found["error"]


def test_children_are_wrapped_in_prlimit_and_the_language_is_validated(monkeypatch):
    if shutil.which("prlimit"):
        wrapped = ocr_probe._limited(["tesseract", "x"], 40, 1 << 30)
        assert wrapped[0].endswith("prlimit") and "--cpu=40" in wrapped and wrapped[-2:] == ["tesseract", "x"]
    monkeypatch.setattr(ocr_probe.shutil, "which", lambda name: None)
    assert ocr_probe._limited(["tesseract", "x"], 40, 1 << 30) == ["tesseract", "x"]
    for good in ("eng", "eng+nld", "chi_sim"):
        monkeypatch.setattr(settings, "verification_ocr_languages", good)
        assert ocr_probe._languages() == good
    for hostile in ("eng; rm -rf /", "../../etc/passwd", "-c tessedit_write_images=1", ""):
        monkeypatch.setattr(settings, "verification_ocr_languages", hostile)
        assert ocr_probe._languages() == "eng"


def test_extraction_uses_ocr_only_for_a_pdf_with_no_text_layer(monkeypatch, tmp_path):
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    calls = []
    monkeypatch.setattr(ocr_probe, "ocr_pdf_front", lambda path: calls.append(path) or {"text": "TITLE PAGE", "pages": 1})
    monkeypatch.setattr(ebook_extraction.pdf_probe, "extract_pdf_text",
                        lambda path, **kw: {"title": "", "author": "", "text": "", "front_text": ""})
    extracted = ebook_extraction.extract_ebook_identity(str(pdf))
    assert calls and extracted.text == "TITLE PAGE" and extracted.source == "native-pdf+ocr"
    # A PDF that already has plenty of text is never OCR'd.
    calls.clear()
    monkeypatch.setattr(ebook_extraction.pdf_probe, "extract_pdf_text",
                        lambda path, **kw: {"title": "", "author": "", "text": "word " * 400, "front_text": "word"})
    extracted = ebook_extraction.extract_ebook_identity(str(pdf))
    assert not calls and extracted.source == "native-pdf" and extracted.ocr is None


def test_image_only_pdfs_verified_before_ocr_are_looked_at_again_once(monkeypatch):
    monkeypatch.setattr(settings, "verification_ocr", True)
    old = {"source": "native-pdf", "verdict": "INSUFFICIENT_EVIDENCE",
           "evidence": {"content": {"text_characters_examined": 0}}}
    assert predates_ocr(old)
    assert not predates_ocr({**old, "evidence": {**old["evidence"], "ocr": {"attempted": True}}})   # already tried
    assert not predates_ocr({**old, "evidence": {"content": {"text_characters_examined": 90000}}})  # has text
    assert not predates_ocr({**old, "source": "native-epub"})
    monkeypatch.setattr(settings, "verification_ocr", False)
    assert not predates_ocr(old)


# --- OCR-tolerant matching: lost spaces and a misread letter, never a different book -----------


def _scan(expected_title, expected_author, page, *, file_title="", file_author=""):
    """Classify a scan whose text came from OCR (the flag the verifier sets for '+ocr' sources)."""
    result = {"title": expected_title, "author": expected_author, "book_id": 1, "series": [],
              "isbn_evidence": {}, "_ocr_text": True}
    return classify_identity(result, {"title": file_title, "author": file_author}, page, [], [], page)


def _signal(title, author, page):
    from app.verification_engine import _identity_signal
    return _identity_signal(title, author, page, page, True)


def test_ocr_text_with_a_lost_space_still_finds_the_title_and_author():
    page = "HOW TO STEALA\nDRAGON'S SWORD\nCRESSIDA COWELL"
    signal = _signal("How to Steal a Dragon's Sword", "Cressida Cowell", page)
    assert signal["title_found"] and signal["author_found"] and signal["strong_identity"]
    # Without the OCR flag the same text is (rightly) not an exact match.
    from app.verification_engine import _identity_signal
    assert not _identity_signal("How to Steal a Dragon's Sword", "Cressida Cowell", page, page)["title_found"]


def test_a_misread_letter_is_near_but_never_found_or_strong():
    page = "HOW TO STEAL A DRAGON'S SWORD\nCRESSIDA COWELI"
    signal = _signal("How to Steal a Dragon's Sword", "Cressida Cowell", page)
    assert signal["title_found"] and not signal["author_found"]
    assert signal["author_near"] and signal["strong_identity"] is False


def test_a_similar_title_is_never_evidence_for_the_expected_book():
    # One letter from "The Last Colony", by a different author, on a file whose metadata copies the catalogue.
    for page in ("THE LOST COLONY\nEOIN COLFER", "THE LOST COLONY\nJOHN SCALZI"):
        verdict, confidence, evidence = _scan("The Last Colony", "John Scalzi", page,
                                              file_title="The Last Colony", file_author="John Scalzi")
        assert verdict != "VERIFIED_CORRECT", (page, verdict, confidence)
        assert verdict != "METADATA_ERROR"
    signal = _signal("The Last Colony", "John Scalzi", "THE LOST COLONY\nJOHN SCALZI")
    assert signal["title_near"] and not signal["title_found"] and not signal["strong_identity"]


def test_the_author_alone_never_verifies_a_scan():
    """A different book by the same author must not verify just because the metadata copies the catalogue."""
    verdict, _, _ = _scan("Zoes Tale Of A Girl", "John Scalzi", "SOMETHING ENTIRELY DIFFERENT\nJOHN SCALZI",
                          file_title="Zoes Tale Of A Girl", file_author="John Scalzi")
    assert verdict != "VERIFIED_CORRECT"
    verdict, confidence, _ = _scan("The Last Colony", "John Scalzi", "THE LAST COLONY\nA NOVEL\nJOHN SCALZI",
                                   file_title="The Last Colony", file_author="John Scalzi")
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 99)


@pytest.mark.parametrize("title, author, page", [
    ("Les Miserables Volume I", "Victor Hugo", "LES MISERABLES\nVOLUME II\nVICTOR HUGO"),
    ("Les Miserables Volume 1", "Victor Hugo", "LES MISERABLES\nVOLUME 12\nVICTOR HUGO"),
    ("Alex Rider Book Four", "Anthony Horowitz", "ALEX RIDER BOOK FOURTEEN\nANTHONY HOROWITZ"),
    ("The Silent Patient", "Alex Michaelides", "THE SILENT PATIENTS\nALEX MICHAELIDES"),
    ("Interview With The Vampire", "Anne Rice", "INTERVIEW WITH THE VAMPIRE\nJOANNE RICE"),
    ("Inferno", "Dan Brown", "INFERNO\nBRENDAN BROWN"),
    ("The Time Machine", "H. G. Wells", "THE TIME MACHINERY\nH. G. WELLS"),
])
def test_an_exact_match_must_stand_alone_not_sit_inside_a_longer_word_or_number(title, author, page):
    signal = _signal(title, author, page)
    assert not (signal["title_found"] and signal["author_found"])
    assert signal["strong_identity"] is False
    assert _scan(title, author, page)[0] not in ("METADATA_ERROR", "VERIFIED_CORRECT")


def test_exact_matching_still_accepts_what_stands_alone_with_other_punctuation():
    for page in ("THE LAST COLONY: A NOVEL\nJOHN SCALZI", "the last colony\n\nby John Scalzi", "THE-LAST-COLONY  JOHN SCALZI"):
        assert _signal("The Last Colony", "John Scalzi", page)["strong_identity"], page
    assert _signal("The 13-Storey Treehouse", "Andy Griffiths", "THE 13 STOREY TREEHOUSE\nANDY GRIFFITHS")["strong_identity"]


def test_ocr_tolerance_never_turns_one_book_into_another():
    def near(title, page):
        return _signal(title, "Cressida Cowell", page)["title_near"]

    page = "HOW TO CHEAT A DRAGON'S CURSE CRESSIDA COWELL"
    assert not near("How to Steal a Dragon's Sword", page)                  # a different book by the same author
    assert not near("The 13-Storey Treehouse", "THE 26-STOREY TREEHOUSE")    # digits must match exactly
    assert not near("Kill", "SKILLED KILLERS")                               # short titles are never near-matched
    assert not near("A Book Nobody Has", "SOMETHING ELSE ENTIRELY WRITTEN HERE")
    assert near("How to Steal a Dragon's Sword", "HOW TO STEAL A DRAGON'S SWORD")
    assert near("How to Steal a Dragon's Sword", "HOW TO STEAL A DRAGONS SW0RD")


def test_a_same_author_also_by_list_is_not_a_title_page():
    """The back matter of another Scalzi book names the expected title next to the author."""
    page = ("ALSO BY JOHN SCALZI\nOld Man's War\nThe Ghost Brigades\nThe Last Colony\nZOE'S TALE\f"
            "ZOE'S TALE\nJOHN SCALZI\nA TOR BOOK")
    assert _signal("The Last Colony", "John Scalzi", page)["strong_identity"] is False
    verdict, _, _ = _scan("The Last Colony", "John Scalzi", page, file_title="Book to Do 1", file_author="Unknown")
    assert verdict not in ("METADATA_ERROR", "VERIFIED_CORRECT")
    # ...but the same title on the title page, before or after a list page, still counts.
    page = "THE LAST COLONY\nJOHN SCALZI\fALSO BY JOHN SCALZI\nOld Man's War\nZOE'S TALE"
    assert _signal("The Last Colony", "John Scalzi", page)["strong_identity"]
    page = "ALSO BY JOHN SCALZI\nOld Man's War\fTHE LAST COLONY\nJOHN SCALZI"
    assert _signal("The Last Colony", "John Scalzi", page)["strong_identity"]


def test_a_short_real_title_in_the_files_own_metadata_is_recognised_so_it_can_dissent():
    """Short embedded titles ("Old Man's War") used to be unsearchable, which let a list entry win."""
    page = "ALSO BY JOHN SCALZI\nThe Ghost Brigades\nThe Last Colony\fOLD MAN'S WAR\nJOHN SCALZI\nA TOR BOOK"
    verdict, _, evidence = _scan("The Last Colony", "John Scalzi", page,
                                 file_title="Old Man's War", file_author="John Scalzi")
    assert verdict != "METADATA_ERROR"
    assert evidence["content"]["embedded_signal"]["strong_identity"] is True


def test_a_file_whose_own_identity_was_misread_by_ocr_is_not_rewritten_to_another_book():
    # The page prints the file's own identity, but OCR read a letter wrong ("WAF"), and the expected
    # title is printed with the author in another block that is not labelled as a list.
    page = "OLD MAN S WAF\nJOHN SCALZI\n\nTHE LAST COLONY\nJOHN SCALZI"
    verdict, _, evidence = _scan("The Last Colony", "John Scalzi", page,
                                 file_title="Old Man's War", file_author="John Scalzi")
    assert evidence["content"]["embedded_signal"]["near_identity"] is True
    assert verdict != "METADATA_ERROR"
    # With the file's identity absent from the page altogether, the old behaviour is unchanged.
    verdict, _, _ = _scan("The Last Colony", "John Scalzi", "THE LAST COLONY\nJOHN SCALZI",
                          file_title="Old Man's War", file_author="John Scalzi")
    assert verdict == "METADATA_ERROR"


# --- the layouts a second review used to defeat the list rule -----------------------------------

LIST_PAGES = [
    "ALSO BY JOHN SCALZI\nOld Man's War\nThe Ghost Brigades\nThe Last Colony",                     # heading first
    "ili\nALSO BY JOHN SCALZI\n\nOld Man's War\nThe Last Colony",                                  # a folio above it
    "ALSO BY\n\nJOHN SCALZI\n\nOld Man's War\n\nThe Last Colony",                                # split heading
    "ALSO BY JOHN SCALZI\n\nOld Man's War\n\nThe Ghost Brigades\n\nThe Human Division\n\nThe Last Colony",
    "ALS0 BY JOHN SCALZI\nOld Man's War\nThe Last Colony",                                         # 0 read for o
    "AL5O BY JOHN SCALZI\nOld Man's War\nThe Last Colony",
    "OTHER BOOKS BY JOHN SCALZI\nThe Last Colony",
    "MORE BOOKS IN THE OLD MAN'S WAR SERIES\nThe Last Colony",
    "PRAISE FOR JOHN SCALZI\n\"Brilliant\" - The Last Colony review",
    "ABOUT THE AUTHOR\nJohn Scalzi wrote Old Man's War and The Last Colony.",
    "Also available\nThe Last Colony\nJohn Scalzi",
]


@pytest.mark.parametrize("list_page", LIST_PAGES)
def test_a_list_page_is_set_aside_wherever_its_heading_and_whatever_its_length(list_page):
    from app.scan_text import without_book_lists
    text, affected = without_book_lists("ZOE'S TALE\fZOE'S TALE\nJOHN SCALZI\nA TOR BOOK\f" + list_page)
    assert affected == 1 and "Last Colony" not in text and "ZOE'S TALE\nJOHN SCALZI" in text
    # ...whichever page the list is on, and the real title page is untouched.
    text, affected = without_book_lists(list_page + "\fTHE LAST COLONY\nJOHN SCALZI")
    assert affected == 1 and "THE LAST COLONY\nJOHN SCALZI" in text and "Old Man" not in text
    for position in range(3):
        pages = ["ZOE'S TALE", "ZOE'S TALE\nJOHN SCALZI"]
        pages.insert(position, list_page)
        verdict, _, _ = _scan("The Last Colony", "John Scalzi", "\f".join(pages),
                              file_title="The Last Colony", file_author="John Scalzi")
        assert verdict not in ("VERIFIED_CORRECT", "METADATA_ERROR"), (position, list_page)


def test_a_cover_cut_at_its_heading_keeps_what_is_above_it():
    from app.scan_text import without_book_lists
    text, affected = without_book_lists("THE LAST COLONY\nJOHN SCALZI\nALSO BY JOHN SCALZI\nOld Man's War")
    assert affected == 1 and text == "THE LAST COLONY\nJOHN SCALZI"
    assert without_book_lists("") == ("", 0) and without_book_lists("THE LAST COLONY") == ("THE LAST COLONY", 0)


@pytest.mark.parametrize("title, author, page", [
    ("Dune", "Frank Herbert", "DUNE MESSIAH\nFRANK HERBERT"),                                    # a longer title
    ("It", "Stephen King", "Copyright 1986 by Stephen King. It is a crime to copy it."),         # a word in prose
    ("Emma", "Jane Austen", "EMMA WOODHOUSE\nJANE AUSTEN"),
    ("Home", "Toni Morrison", "WELCOME HOME\nTONI MORRISON"),
    ("The Stand", "Stephen King", "WE TAKE A STAND\nSTEPHEN KING"),
    ("Beloved", "Toni Morrison", "WE ARE BE LOVED BY ALL\nTONI MORRISON"),
])
def test_a_title_must_fill_its_own_lines_not_sit_inside_a_longer_title_or_a_sentence(title, author, page):
    assert _signal(title, author, page)["title_found"] is False
    assert _signal(title, author, page)["strong_identity"] is False
    assert _scan(title, author, page)[0] not in ("METADATA_ERROR", "VERIFIED_CORRECT")


def test_the_generic_subtitle_alone_is_never_a_title():
    page = "A NOVEL\nBY JOHN SCALZI"
    for generic in ("A Novel", "Novel", "A Memoir", "A Thriller"):
        assert _signal(generic, "John Scalzi", page)["title_found"] is False


def test_a_titles_own_line_may_carry_an_article_a_generic_tail_the_author_or_nothing():
    for page in ("THE LAST COLONY", "THE LAST COLONY: A NOVEL", "THE LAST COLONY\nA NOVEL",
                 "THE LAST COLONY BY JOHN SCALZI", "THE LAST COLONY JOHN SCALZI", "THE LAST\nCOLONY"):
        assert _signal("The Last Colony", "John Scalzi", page + "\nJOHN SCALZI")["title_found"], page
    for page in ("THE LAST COLONY OF MARS", "NOW IN PAPERBACK THE LAST COLONY", "A THE LAST COLONY", "LAST COLONY"):
        assert _signal("The Last Colony", "John Scalzi", page + "\nJOHN SCALZI")["title_found"] is False, page


def test_the_title_and_the_author_must_be_on_the_same_page_and_close_together():
    far = "THE LAST COLONY\n" + ("A LONG PARAGRAPH ABOUT SOMETHING ELSE ENTIRELY. " * 12) + "\nJOHN SCALZI"
    assert _signal("The Last Colony", "John Scalzi", far)["strong_identity"] is False
    assert _signal("The Last Colony", "John Scalzi", "THE LAST COLONY\fJOHN SCALZI")["strong_identity"] is False
    assert _signal("The Last Colony", "John Scalzi", "THE LAST COLONY\nJOHN SCALZI")["strong_identity"] is True


def test_for_a_scan_the_title_and_the_author_must_both_be_found_to_verify():
    # Same title, another author (a different book of that name) with metadata copying the catalogue.
    for page in ("THE LAST COLONY\nEOIN COLFER", "THE LAST COLONY"):
        verdict, _, _ = _scan("The Last Colony", "John Scalzi", page,
                              file_title="The Last Colony", file_author="John Scalzi")
        assert verdict != "VERIFIED_CORRECT", page


@pytest.fixture
def scalzi_bindery(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "bindery.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER, excluded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT, path TEXT, size_bytes INTEGER);
            CREATE TABLE history (id INTEGER PRIMARY KEY, book_id INTEGER, event_type TEXT, source_title TEXT, data TEXT, created_at TEXT);
            INSERT INTO authors VALUES (1, 'John Scalzi');
            INSERT INTO books(id, title, author_id) VALUES (1, 'Zoe''s Tale', 1), (2, 'Old Man''s War', 1),
                                                           (3, 'The Last Colony', 1);
            INSERT INTO history(book_id, event_type, source_title) VALUES
              (3, 'bookImported', 'John Scalzi - The Last Colony (retail) (pdf)');
            """
        )
    monkeypatch.setattr(settings, "bindery_db", str(path))
    return path


SIBLING_SCAN = "\f".join(["ZOE'S TALE", "ALSO BY JOHN SCALZI\nOld Man's War\nThe Ghost Brigades\nThe Last Colony",
                          "ZOE'S TALE\nJOHN SCALZI\nA TOR BOOK"])


def test_a_books_also_by_page_never_makes_it_another_book_or_a_move(scalzi_bindery):
    """The genuine Zoe's Tale, tagged (wrongly) as Old Man's War, must not be 'moved' to Old Man's War."""
    result = {"title": "Zoe's Tale", "author": "John Scalzi", "book_id": 1, "series": [], "isbn_evidence": {},
              "_ocr_text": True}
    verdict, _, evidence = classify_identity(result, {"title": "Old Man's War", "author": "John Scalzi"},
                                             SIBLING_SCAN, [], [], SIBLING_SCAN)
    assert verdict != "WRONG_CONTENT" and "actualBook" not in evidence
    assert evidence["content"]["embedded_signal"]["strong_identity"] is False


def test_a_sibling_scan_with_the_right_metadata_or_none_is_not_the_assigned_book(scalzi_bindery):
    result = {"title": "The Last Colony", "author": "John Scalzi", "book_id": 3, "series": [], "isbn_evidence": {},
              "_ocr_text": True}
    for metadata in ({"title": "The Last Colony", "author": "John Scalzi"}, {"title": "", "author": ""},
                     {"title": "Book to Do 1", "author": "Unknown"},
                     {"title": "Zoe's Tale (Old Man's War, Book 4)", "author": "John Scalzi"}):
        verdict, confidence, _ = classify_identity(result, metadata, SIBLING_SCAN, [], [], SIBLING_SCAN)
        assert verdict not in ("VERIFIED_CORRECT", "METADATA_ERROR"), (metadata, verdict, confidence)


def test_the_download_name_plus_the_authors_name_alone_does_not_prove_a_scan(scalzi_bindery):
    """Provenance counted 'the book's text' when only the author was on the page."""
    result = {"title": "The Last Colony", "author": "John Scalzi", "book_id": 3, "series": [], "isbn_evidence": {},
              "_ocr_text": True}
    page = "ZOE'S TALE\nJOHN SCALZI\nA TOR BOOK"
    verdict, _, evidence = classify_identity(result, {"title": "The Last Colony", "author": "John Scalzi"},
                                             page, [], [], page)
    assert verdict == "INSUFFICIENT_EVIDENCE" and "proof" not in evidence
    # The same evidence for the genuine book (title and author on the page) is accepted.
    page = "THE LAST COLONY\nJOHN SCALZI\nA TOR BOOK"
    verdict, confidence, _ = classify_identity(result, {"title": "The Last Colony", "author": "John Scalzi"},
                                               page, [], [], page)
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 99)


def scan_pdf(path, pages, *, metadata=None):
    """A multi-page image-only PDF: each page is a list of lines drawn as pixels."""
    pil = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    font_module = pytest.importorskip("PIL.ImageFont")
    font = font_module.load_default(size=54)
    images = []
    for lines in pages:
        image = pil.new("L", (1275, 1650), 255)
        draw = draw_module.Draw(image)
        y = 300
        for line in lines:
            draw.text((120, y), line, fill=0, font=font)
            y += 100
        images.append(image)
    images[0].save(path, "PDF", resolution=150, save_all=True, append_images=images[1:], **(metadata or {}))
    return path


@NEEDS_TOOLS
def test_a_real_multi_page_scan_of_a_sibling_book_does_not_verify_through_its_also_by_page(tmp_path):
    pdf = scan_pdf(tmp_path / "scan.pdf", [
        ["ZOE'S TALE"],
        ["ALSO BY JOHN SCALZI", "Old Man's War", "The Ghost Brigades", "The Human Division", "The Last Colony"],
        ["ZOE'S TALE", "JOHN SCALZI", "A TOR BOOK"],
    ], metadata={"title": "The Last Colony", "author": "John Scalzi"})
    extracted = ebook_extraction.extract_ebook_identity(str(pdf))
    assert extracted.source == "native-pdf+ocr" and extracted.ocr["setAside"] == 1
    assert "colony" not in extracted.text.lower().replace("\f", " ") and "\f" in extracted.text
    assert any("set aside" in note for note in extracted.notes)
    verdict, confidence, _ = _classify_scan(pdf, "The Last Colony", "John Scalzi")
    assert verdict not in {"VERIFIED_CORRECT", "METADATA_ERROR"}, (verdict, confidence)


@NEEDS_TOOLS
def test_a_real_multi_page_scan_of_the_right_book_still_verifies_with_an_also_by_page(tmp_path):
    pdf = scan_pdf(tmp_path / "scan.pdf", [
        ["THE LAST COLONY"],
        ["ALSO BY JOHN SCALZI", "Old Man's War", "The Ghost Brigades"],
        ["THE LAST COLONY", "JOHN SCALZI", "A TOR BOOK"],
    ], metadata={"title": "The Last Colony", "author": "John Scalzi"})
    assert _classify_scan(pdf, "The Last Colony", "John Scalzi")[:2] == ("VERIFIED_CORRECT", 99)


@NEEDS_TOOLS
def test_a_scan_that_is_only_a_list_reports_why_instead_of_inventing_a_title(tmp_path):
    pdf = scan_pdf(tmp_path / "scan.pdf", [["ALSO BY JOHN SCALZI", "Old Man's War", "The Last Colony"]])
    found = ocr_probe.ocr_pdf_front(pdf)
    assert "text" not in found and "list other books" in found["error"] and found["setAside"] == 1


def test_ocr_search_work_is_bounded_by_the_text_and_the_name(monkeypatch):
    """Deterministic: counts close comparisons, not seconds. A name can be any length (a PDF /Title)."""
    from app import verification_engine as engine
    calls = []
    real = engine.SequenceMatcher

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "SequenceMatcher", counting)
    ceiling = engine.OCR_MAX_CANDIDATES * (2 * engine.OCR_CANDIDATE_SLACK + 1)

    huge = engine._Squashed("hownotostealadragonsworx " * 40_000)    # 1,000,000 characters, nearly the title
    assert len(huge.text) == engine.OCR_MAX_CHARS and len(huge.origin) == engine.OCR_MAX_CHARS
    engine._ocr_near_pos("howtostealadragonssword", huge, engine.OCR_MIN_NEAR)       # too common to anchor on
    assert len(calls) <= ceiling

    calls.clear()
    repeated = engine._Squashed("hownotostealadragonsworx " * 150)   # many look-alike places to compare
    engine._ocr_near_pos("howtostealadragonssword", repeated, engine.OCR_MIN_NEAR)
    assert 0 < len(calls) <= ceiling

    calls.clear()
    noise = engine._Squashed("ab " * 20_000)                        # low-diversity text defeats most matchers
    for size in (20, 80, 300, 3000):
        engine._ocr_near_pos(("ab" * size)[:size], noise, engine.OCR_MIN_NEAR)
    assert len(calls) <= 4 * ceiling                                # needles over the cap are not compared at all

    calls.clear()
    assert engine._ocr_near_pos("x" * (engine.OCR_MAX_NEAR_NEEDLE + 1), noise, 1) == -1 and not calls


def test_a_misread_name_is_found_wherever_it_sits_in_a_long_page():
    """The close comparison is anchored on matching fragments, not on a fixed stride over the text."""
    from app import verification_engine as engine
    for pad in range(0, 25):
        page = "lorem ipsum dolor sit amet " * (150 + pad) + "HOW TO STEAL A DRAGONS SW0RD" + " consectetur" * 200
        squashed = engine._Squashed(page)
        assert engine._ocr_near_pos("howtostealadragonssword", squashed, engine.OCR_MIN_NEAR) >= 0, pad
