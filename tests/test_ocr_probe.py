"""OCR of image-only PDFs: bounded, optional, and only ever able to make a match fail, not invent one."""

from __future__ import annotations

import shutil
import time

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
def test_an_image_only_pdf_with_placeholder_metadata_is_read_but_the_page_alone_is_not_proof(tmp_path):
    pdf = title_page_pdf(tmp_path / "scan.pdf", ["HOW TO STEAL A", "DRAGON'S SWORD", "CRESSIDA COWELL"],
                         metadata={"title": "Book to Do 1", "author": "Unknown"})
    extracted = ebook_extraction.extract_ebook_identity(str(pdf))
    assert extracted.source == "native-pdf+ocr" and extracted.ocr and extracted.ocr["chars"] > 20
    assert any("OCR" in note for note in extracted.notes)
    result = {"title": "How to Steal a Dragon's Sword", "author": "Cressida Cowell", "book_id": 1,
              "series": [], "isbn_evidence": {}, "_ocr_text": extracted.source.endswith("+ocr")}
    verdict, _confidence, evidence = classify_identity(
        result, extracted.metadata, extracted.text, extracted.identifiers, extracted.notes, extracted.front_text)
    # The page itself names the assigned book while the file's own details are junk: that is one source,
    # and a scan's first page is not accepted as proof by itself (a cover can name other books).
    assert evidence["content"]["expected_signal"]["strong_identity"] is True
    assert verdict == "INSUFFICIENT_EVIDENCE" and evidence["scanPage"] == {"readsAs": "expected"}


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
    from app.verification_constants import OCR_VERSION
    current = {**old, "evidence": {**old["evidence"], "ocr": {"attempted": True, "version": OCR_VERSION}}}
    assert not predates_ocr(current)                                                                # already tried
    assert predates_ocr({**old, "evidence": {**old["evidence"], "ocr": {"attempted": True}}})       # an older reader
    assert predates_ocr({**old, "evidence": {**old["evidence"], "ocr": {"attempted": True, "version": 1}}})
    assert predates_ocr({**current, "evidence": {**current["evidence"], "ocr": {"attempted": True, "error": "x"}}})  # legacy OCR outage gets one retry
    assert not predates_ocr({**old, "verdict": "VERIFIED_CORRECT",
                             "evidence": {**old["evidence"], "ocr": {"attempted": True, "version": 1}}})
    assert not predates_ocr({**old, "evidence": {"content": {"text_characters_examined": 90000}}})  # has text
    assert not predates_ocr({**old, "source": "native-epub"})
    monkeypatch.setattr(settings, "verification_ocr", False)
    assert not predates_ocr(old)


@pytest.mark.parametrize("error_code", ["invalid_pdf", "password_protected", "no_text", "no_title_page"])
def test_permanent_ocr_failures_remain_cached(monkeypatch, error_code):
    monkeypatch.setattr(settings, "verification_ocr", True)
    policy = ocr_probe.ocr_policy()
    saved = {"source": "native-pdf", "verdict": "INSUFFICIENT_EVIDENCE",
             "evidence": {"ocr": {"attempted": True, "error": "Failed", "errorCode": error_code,
                                  "policy": policy, "attemptedAt": time.time() - 86400}}}
    assert not predates_ocr(saved)


@pytest.mark.parametrize("error_code", [
    "dependency_unavailable", "preflight_io_error", "render_timeout",
    "render_output_limit", "render_execution_error", "render_error", "ocr_execution_error",
])
def test_transient_ocr_failures_have_a_cooldown(monkeypatch, error_code):
    monkeypatch.setattr(settings, "verification_ocr", True)
    from app.verifier import OCR_TRANSIENT_RETRY_SECONDS
    saved = {"source": "native-pdf", "verdict": "INSUFFICIENT_EVIDENCE",
             "evidence": {"ocr": {"attempted": True, "error": "Temporary error", "errorCode": error_code,
                                  "policy": ocr_probe.ocr_policy(), "attemptedAt": time.time()}}}
    assert not predates_ocr(saved)
    saved["evidence"]["ocr"]["attemptedAt"] -= OCR_TRANSIENT_RETRY_SECONDS + 2
    assert predates_ocr(saved)
    # Changing settings must not wait for the cooldown.
    saved["evidence"]["ocr"]["attemptedAt"] = time.time()
    monkeypatch.setattr(settings, "verification_ocr_pages", int(settings.verification_ocr_pages) + 1)
    assert predates_ocr(saved)


def test_ocr_cache_rechecks_when_disabled_without_repeating(monkeypatch):
    monkeypatch.setattr(settings, "verification_ocr", False)
    assert predates_ocr({"source": "native-pdf+ocr", "verdict": "VERIFIED_CORRECT",
                         "evidence": {"ocr": {"attempted": True}}})
    assert not predates_ocr({"source": "native-pdf", "verdict": "INSUFFICIENT_EVIDENCE",
                             "evidence": {"content": {"text_characters_examined": 0}}})


def test_preflight_distinguishes_invalid_content_from_io_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "verification_ocr", True)
    monkeypatch.setattr(ocr_probe.shutil, "which", lambda name: "/usr/bin/" + name)
    bad = tmp_path / "invalid.pdf"
    bad.write_bytes(b"not a PDF")
    assert ocr_probe.ocr_pdf_front(bad)["error_code"] == "invalid_pdf"

    def unavailable(_path):
        raise OSError("Temporary I/O failure")

    monkeypatch.setattr(ocr_probe, "_preflight", unavailable)
    assert ocr_probe.ocr_pdf_front(bad)["error_code"] == "preflight_io_error"


@pytest.mark.parametrize("diagnostic, expected", [
    (b"Command Line Error: Incorrect password", "password_protected"),
    (b"Error: resource temporarily unavailable", "render_error"),
])
def test_pdf_render_exit_code_is_classified_conservatively(tmp_path, monkeypatch, diagnostic, expected):
    from app.subprocess_capture import BoundedProcessResult

    monkeypatch.setattr(settings, "verification_ocr", True)
    monkeypatch.setattr(ocr_probe.shutil, "which", lambda name: "/usr/bin/" + name)
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\\n%%EOF\\n")
    monkeypatch.setattr(ocr_probe, "run_bounded_process",
                        lambda *_args, **_kwargs: BoundedProcessResult(1, b"", diagnostic))
    outcome = ocr_probe.ocr_pdf_front(pdf)
    assert outcome["error_code"] == expected
    assert outcome["policy"] == ocr_probe.ocr_policy()


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
    # No exact author credit means no title page, so neither counts as found; both are only "near".
    assert not signal["title_found"] and not signal["author_found"]
    assert signal["title_near"] and signal["author_near"] and signal["strong_identity"] is False


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
    # With the file's identity absent from the page altogether the page still reads as the assigned
    # book, but one source is not enough to call the file's details wrong.
    verdict, _, evidence = _scan("The Last Colony", "John Scalzi", "THE LAST COLONY\nJOHN SCALZI",
                                 file_title="Old Man's War", file_author="John Scalzi")
    assert verdict == "INSUFFICIENT_EVIDENCE" and evidence["scanPage"] == {"readsAs": "expected"}


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


def scan_pdf(path, pages, *, metadata=None, size=54, gap=100, top=300):
    """A multi-page image-only PDF: each page is a list of lines drawn as pixels."""
    pil = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    font_module = pytest.importorskip("PIL.ImageFont")
    font = font_module.load_default(size=size)
    images = []
    for lines in pages:
        image = pil.new("L", (1275, 1650), 255)
        draw = draw_module.Draw(image)
        y = top
        for line in lines:
            draw.text((120, y), line, fill=0, font=font)
            y += gap
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


# --- the third review: pages no heading list can name ----------------------------------------------

SIBLING_TITLE_PAGE = "ZOE'S TALE\nJOHN SCALZI\nA TOR BOOK"
UNLISTED_PAGES = [
    "COMING SOON\nTHE LAST COLONY\nJOHN SCALZI",
    "DON'T MISS\nTHE LAST COLONY\nBY JOHN SCALZI",
    "THE OLD MAN'S WAR SERIES\nby John Scalzi\nOld Man's War\nThe Ghost Brigades\nThe Last Colony",
    "JOHN SCALZI\n\nOld Man's War\nThe Ghost Brigades\nThe Last Colony\nZoe's Tale",      # an author-name heading
    "JOHN SCALZI\nNOVELS\nOld Man's War\nThe Ghost Brigades\nThe Last Colony",
    "CONTENTS\nTHE LAST COLONY\nZOE'S TALE",
    "BOOK THREE\nTHE LAST COLONY\nJOHN SCALZI",
    "JOHN SCALZI\nTHE LAST COLONY\n3",                                                     # a running header
    "For John Scalzi\nTHE LAST COLONY",                                                    # a dedication
    "From the bestselling author of\nTHE LAST COLONY\ncomes a new adventure from\nJOHN SCALZI",
    "JOHN SCALZI (CONTINUED)\nTHE LAST COLONY\nZOE'S TALE",
    "CRITICAL ACCLAIM FOR\nJOHN SCALZI\nTHE LAST COLONY",
    "REVIEWS\nTHE LAST COLONY\nJOHN SCALZI",
    "NEXT IN THE SERIES\nTHE LAST COLONY\nJOHN SCALZI",
    "READ MORE\nTHE LAST COLONY\nJOHN SCALZI",
    "A companion to\nTHE LAST COLONY\nJOHN SCALZI",
]


@pytest.mark.parametrize("promo", UNLISTED_PAGES)
def test_a_page_after_the_title_page_can_never_make_a_scan_another_book(promo, scalzi_bindery):
    """A sibling's scan is the book its title page names; whatever it prints later cannot say otherwise."""
    pages = "\f".join(["ZOE'S TALE", SIBLING_TITLE_PAGE, promo])
    for metadata in ({"title": "The Last Colony", "author": "John Scalzi"}, {"title": "", "author": ""},
                     {"title": "Book to Do 1", "author": "Unknown"}):
        verdict, confidence, _ = _scan("The Last Colony", "John Scalzi", pages,
                                       file_title=metadata["title"], file_author=metadata["author"])
        assert verdict not in ("VERIFIED_CORRECT", "METADATA_ERROR"), (promo, metadata, verdict, confidence)
    # ...and a promotion for ANOTHER book of the author's cannot make the file that other book, nor
    # send the genuine book to it (the second pass names a catalogue book only from a title page).
    other = promo.replace("THE LAST COLONY", "OLD MAN'S WAR").replace("Last Colony", "Old Man's War")
    result = {"title": "Zoe's Tale", "author": "John Scalzi", "book_id": 1, "series": [], "isbn_evidence": {},
              "_ocr_text": True}
    verdict, _, evidence = classify_identity(
        result, {"title": "Old Man's War", "author": "John Scalzi"},
        "\f".join(["ZOES TAL", SIBLING_TITLE_PAGE, other]), [], [], "")
    assert verdict != "WRONG_CONTENT" and "actualBook" not in evidence


def test_a_promotion_that_credits_the_author_before_the_title_page_cannot_verify_a_scan_alone():
    """Accepted limit: the first page that credits the author is taken as the title page. A promotion
    printed before the real one is rare, and a second source still has to agree."""
    pages = "\f".join(["ZOE'S TALE", "COMING SOON\nTHE LAST COLONY\nJOHN SCALZI", SIBLING_TITLE_PAGE])
    for metadata in ({"title": "", "author": ""}, {"title": "Book to Do 1", "author": "Unknown"}):
        verdict, _, evidence = _scan("The Last Colony", "John Scalzi", pages,
                                     file_title=metadata["title"], file_author=metadata["author"])
        assert verdict == "INSUFFICIENT_EVIDENCE" and evidence["scanPage"] == {"readsAs": "expected"}


def test_the_titles_must_open_the_scan_but_a_half_title_and_a_cover_both_do():
    assert _signal("The Last Colony", "John Scalzi", "THE LAST COLONY\fTHE LAST COLONY\nJOHN SCALZI\nA TOR BOOK")["strong_identity"]
    assert _signal("The Last Colony", "John Scalzi", "#1 NEW YORK TIMES BESTSELLER\nTHE LAST COLONY\nJOHN SCALZI")["strong_identity"]
    # a first page whose lines run on long enough pushes the title out of the opening
    filler = "\n".join(f"A LINE OF TEXT NUMBER {n}" for n in range(12))
    assert _signal("The Last Colony", "John Scalzi", filler + "\nTHE LAST COLONY\nJOHN SCALZI")["title_found"] is False


@pytest.mark.parametrize("heading", [
    "ALSOBY JOHNSCALZI", "A L S O  B Y", "ALSO\nBY\nJOHN SCALZI", "FROM THE SAME AUTHOR", "MORE BY JOHN SCALZI",
    "MORE FROM JOHN SCALZI", "ALSO FROM JOHN SCALZI", "ALSO IN THIS SERIES", "ACCLAIM FOR JOHN SCALZI",
    "AL5O BY JOHN SCALZI", "3 ALSO BY JOHN SCALZI", "OTHER\nBOOKS BY JOHN SCALZI",
])
def test_headings_read_as_ocr_actually_reads_them_still_set_the_page_aside(heading):
    from app.scan_text import without_book_lists
    text, affected = without_book_lists(f"{heading}\nOld Man's War\nThe Last Colony")
    assert affected == 1 and "Last Colony" not in text


@pytest.mark.parametrize("line", [
    "THE LAST COLONY\nA Novel by John Scalzi", "THE LAST COLONY\nA NOVEL BY\nJOHN SCALZI",
    "BOOK 3 IN THE OLD MAN'S WAR SERIES\nTHE LAST COLONY\nJOHN SCALZI", "LOOK FOR ME\nJOHN SCALZI",
    "ALSO KNOWN AS\nJOHN SCALZI", "THE LAST COLONY\nJOHN SCALZI\nA TOR BOOK",
])
def test_real_title_page_lines_are_not_mistaken_for_list_headings(line):
    from app.scan_text import without_book_lists
    assert without_book_lists(line) == (line, 0)


@pytest.mark.parametrize("title, author, page", [
    ("Love", "Erich Segal", "LOVE STORY\nERICH SEGAL"),
    ("Taken", "Benedict Jacka", "TAKEN BY STORM\nBENEDICT JACKA"),
    ("Twilight", "Stephenie Meyer", "THE TWILIGHT SAGA\nECLIPSE\nSTEPHENIE MEYER"),
    ("Fairy", "Stephen King", "FAIRY TALE\nSTEPHEN KING"),
    ("The Last Colony", "John Scalzi", "THE LAST COLONY by Eoin Colfer\nJOHN SCALZI"),
])
def test_a_longer_title_is_not_the_assigned_one_whatever_follows_it(title, author, page):
    assert _signal(title, author, page)["title_found"] is False
    assert _scan(title, author, page, file_title=title, file_author=author)[0] != "VERIFIED_CORRECT"


@pytest.mark.parametrize("credit", [
    "Foreword by John Scalzi", "Translated by John Scalzi", "JOHN SCALZI JR.", "JOHN SCALZI-SMITH",
    "With an introduction by John Scalzi", '"A thrilling ride" - John Scalzi', "A Study Guide for John Scalzi",
])
def test_the_author_is_credited_as_the_author_not_named_in_some_other_role(credit):
    page = f"THE LAST COLONY\nEOIN COLFER\n{credit}"
    assert _signal("The Last Colony", "John Scalzi", page)["author_found"] is False
    assert _scan("The Last Colony", "John Scalzi", page, file_title="The Last Colony",
                 file_author="John Scalzi")[0] != "VERIFIED_CORRECT"


def test_co_authors_and_configured_aliases_are_credited_like_the_authors_themselves():
    assert _signal("Good Omens", "Neil Gaiman; Terry Pratchett", "GOOD OMENS\nTERRY PRATCHETT")["strong_identity"]
    assert _signal("Good Omens", "Neil Gaiman; Terry Pratchett", "GOOD OMENS\nNEIL GAIMAN AND TERRY PRATCHETT")["strong_identity"]
    # the default alias list says Robert Galbraith is J.K. Rowling
    assert _signal("The Cuckoo's Calling", "J.K. Rowling", "THE CUCKOO'S CALLING\nROBERT GALBRAITH")["strong_identity"]
    assert _scan("The Cuckoo's Calling", "J.K. Rowling", "THE CUCKOO'S CALLING\nROBERT GALBRAITH",
                 file_title="The Cuckoo's Calling", file_author="J.K. Rowling")[:2] == ("VERIFIED_CORRECT", 99)


def test_the_title_and_author_must_be_found_together_for_90_as_well_as_99():
    """A contents entry or series header on a later page plus the author elsewhere is not a title page."""
    for pages in ("ZOE'S TALE\nJOHN SCALZI\fCONTENTS\nTHE LAST COLONY\nZOE'S TALE\fx",
                  "ZOE'S TALE\fBOOK THREE\nTHE LAST COLONY\fZOE'S TALE\nJOHN SCALZI\nA TOR BOOK"):
        verdict, _, _ = _scan("The Last Colony", "John Scalzi", pages,
                              file_title="The Last Colony", file_author="John Scalzi")
        assert verdict != "VERIFIED_CORRECT", pages


@NEEDS_TOOLS
def test_a_real_scan_with_an_unlisted_promotion_page_does_not_verify_a_sibling(tmp_path):
    for promo in (["COMING SOON", "THE LAST COLONY", "JOHN SCALZI"],
                  ["THE OLD MAN'S WAR SERIES", "by John Scalzi", "Old Man's War", "The Last Colony"]):
        pdf = scan_pdf(tmp_path / "scan.pdf", [["ZOE'S TALE"], ["ZOE'S TALE", "JOHN SCALZI", "A TOR BOOK"], promo],
                       metadata={"title": "The Last Colony", "author": "John Scalzi"})
        verdict, confidence, _ = _classify_scan(pdf, "The Last Colony", "John Scalzi")
        assert verdict not in {"VERIFIED_CORRECT", "METADATA_ERROR"}, (promo, verdict, confidence)


# --- extraction, the unmatched check and the OCR reader ------------------------------------------

def test_a_short_real_text_layer_is_kept_not_replaced_by_ocr(monkeypatch, tmp_path):
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    calls = []
    monkeypatch.setattr(ocr_probe, "ocr_pdf_front", lambda path: calls.append(path) or {"text": "OCR", "pages": 1})
    monkeypatch.setattr(ebook_extraction.pdf_probe, "extract_pdf_text",
                        lambda path, **kw: {"title": "", "author": "", "text": "THE LAST COLONY\nJOHN SCALZI",
                                            "front_text": "THE LAST COLONY\nJOHN SCALZI"})
    extracted = ebook_extraction.extract_ebook_identity(str(pdf))
    assert not calls and extracted.source == "native-pdf" and extracted.text.startswith("THE LAST COLONY")
    # The re-look rule agrees: a PDF with that much text was never waiting for OCR.
    monkeypatch.setattr(settings, "verification_ocr", True)
    assert not predates_ocr({"source": "native-pdf", "verdict": "VERIFIED_CORRECT",
                             "evidence": {"content": {"text_characters_examined": 29}}})
    assert predates_ocr({"source": "native-pdf", "verdict": "INSUFFICIENT_EVIDENCE",
                         "evidence": {"content": {"text_characters_examined": 0}}})


def _unmatched_stubs(monkeypatch, tmp_path, source):
    from types import SimpleNamespace
    from app import unmatched
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 5000)
    prose = "The Last Colony by John Scalzi. " + "A line of ordinary prose about colonists. " * 200
    monkeypatch.setattr(unmatched, "inspect_ebook_security", lambda *a, **k: {"failures": [], "checks": {}, "inconclusive": False})
    monkeypatch.setattr(unmatched, "_drm_locked", lambda path: False)
    pages = []
    monkeypatch.setattr(unmatched, "_picture_pages", lambda path: pages.append(path) or 1)
    monkeypatch.setattr(unmatched, "extract_ebook_identity", lambda path: SimpleNamespace(
        text=prose, front_text=prose, source=source, metadata={}, identifiers=[]))
    return unmatched, pdf, pages


def test_the_unmatched_check_does_not_read_ocr_text_as_a_title_page_or_a_books_length(monkeypatch, tmp_path):
    """That check matches loosely (a title phrase and a surname anywhere), which is only safe on real text."""
    unmatched, pdf, pages = _unmatched_stubs(monkeypatch, tmp_path, "native-pdf+ocr")
    facts = unmatched._ebook_facts(pdf, [])
    assert facts["front"] == "" and pages           # a scan is judged as before OCR existed: by its pictures
    unmatched, pdf, pages = _unmatched_stubs(monkeypatch, tmp_path, "native-pdf")
    facts = unmatched._ebook_facts(pdf, [])
    assert "last colony" in facts["front"] and not pages


@NEEDS_TOOLS
def test_praise_or_a_list_on_the_first_page_does_not_stop_the_reader_before_the_title_page(tmp_path):
    filler = ["Reviewers agree that this is a remarkable and thoroughly enjoyable novel indeed."] * 44
    pdf = scan_pdf(tmp_path / "scan.pdf", [["PRAISE FOR JOHN SCALZI", *filler], ["THE LAST COLONY", "JOHN SCALZI"],
                                           ["Copyright 2007"]],
                   metadata={"title": "The Last Colony", "author": "John Scalzi"}, size=18, gap=33, top=40)
    found = ocr_probe.ocr_pdf_front(pdf)
    assert found["setAside"] >= 1 and "COLONY" in found["text"].upper()
    assert _classify_scan(pdf, "The Last Colony", "John Scalzi")[:2] == ("VERIFIED_CORRECT", 99)


def test_pages_are_rendered_at_a_bounded_size_whatever_the_page_size(monkeypatch, tmp_path):
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    seen = []

    def fake(command, **kwargs):
        seen.append(command)
        raise OSError("stop")

    monkeypatch.setattr(settings, "verification_ocr", True)
    monkeypatch.setattr(ocr_probe.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ocr_probe, "_preflight", lambda path: None)
    monkeypatch.setattr(ocr_probe, "run_bounded_process", fake)
    ocr_probe.ocr_pdf_front(pdf)
    command = seen[0]
    assert "-scale-to" in command and command[command.index("-scale-to") + 1] == str(ocr_probe.RENDER_MAX_PIXELS)
    assert "-r" not in command          # an absolute size, not a resolution: a huge page cannot cost more


# --- the final attack: two sources, not one cover --------------------------------------------------

CLEAN_COVER = "JOHN SCALZI\nTHE LAST COLONY\nA TOR BOOK"


def test_a_scans_first_page_alone_never_calls_its_details_wrong():
    for metadata in ({"title": "", "author": ""}, {"title": "Book to Do 1", "author": "Unknown"}):
        verdict, confidence, evidence = _scan("The Last Colony", "John Scalzi", "THE LAST COLONY\nJOHN SCALZI",
                                              file_title=metadata["title"], file_author=metadata["author"])
        assert (verdict, confidence) == ("INSUFFICIENT_EVIDENCE", 70) and evidence["scanPage"]["readsAs"] == "expected"
    # With the file's own details agreeing, page and details are two sources.
    assert _scan("The Last Colony", "John Scalzi", "THE LAST COLONY\nJOHN SCALZI",
                 file_title="The Last Colony", file_author="John Scalzi")[:2] == ("VERIFIED_CORRECT", 99)


def test_the_download_record_and_the_first_page_are_two_sources_that_verify_a_scan(scalzi_bindery):
    result = {"title": "The Last Colony", "author": "John Scalzi", "book_id": 3, "series": [], "isbn_evidence": {},
              "_ocr_text": True}
    for metadata in ({"title": "", "author": ""}, {"title": "scan0001", "author": "Unknown"}):
        verdict, confidence, evidence = classify_identity(result, metadata, CLEAN_COVER, [], [], CLEAN_COVER)
        assert (verdict, confidence) == ("VERIFIED_CORRECT", 90), metadata
        assert evidence["proof"]["sources"] == ["download name in Bindery's history", "the scan's title page"]
        assert "Two independent sources" in evidence["explanation"]


def test_a_download_record_for_another_book_or_none_at_all_verifies_nothing(scalzi_bindery):
    # Bindery grabbed a release of THIS book, but the scan opens with another title.
    result = {"title": "The Last Colony", "author": "John Scalzi", "book_id": 3, "series": [], "isbn_evidence": {},
              "_ocr_text": True}
    page = "ZOE'S TALE\nJOHN SCALZI\nA TOR BOOK"
    assert classify_identity(result, {"title": "", "author": ""}, page, [], [], page)[0] == "INSUFFICIENT_EVIDENCE"
    # The scan opens with the assigned title, but Bindery has no download record for this book.
    other = dict(result, book_id=2, title="Old Man's War")
    page = "OLD MAN'S WAR\nJOHN SCALZI\nA TOR BOOK"
    verdict, _, evidence = classify_identity(other, {"title": "", "author": ""}, page, [], [], page)
    assert verdict == "INSUFFICIENT_EVIDENCE" and "proof" not in evidence
    # A record for the same title by a different author does not count either.
    wrong = dict(result, author="Eoin Colfer")
    page = "THE LAST COLONY\nEOIN COLFER"
    assert classify_identity(wrong, {"title": "", "author": ""}, page, [], [], page)[0] == "INSUFFICIENT_EVIDENCE"


def test_a_scan_is_never_moved_to_another_book_without_an_isbn(scalzi_bindery):
    from app.identity_search import second_pass
    evidence = {
        "expected": {"title": "The Ghost Brigades", "author": "John Scalzi"},
        "embedded": {"title": "Old Man's War", "author": "John Scalzi"},
        "content": {"expected_signal": {"ocr": True, "title_found": False, "title_near": False, "strong_identity": False},
                    "embedded_signal": {"ocr": True, "strong_identity": True}},
    }
    assert second_pass({"book_id": 4, "series": []}, evidence) is None
    assert {"check": "scanNeedsIsbnToMove"} in evidence["secondPass"] and "actualBook" not in evidence
    # The same evidence for a text-layer book is allowed to name the other book (existing behaviour)...
    evidence = {"expected": evidence["expected"], "embedded": evidence["embedded"],
                "content": {"expected_signal": {"title_found": False, "title_near": False, "strong_identity": False},
                            "embedded_signal": {"strong_identity": True}}}
    assert second_pass({"book_id": 4, "series": []}, evidence) == ("WRONG_CONTENT", 85)
    # ...and a scan's ISBN naming that same book is the second source that lets it.
    evidence = {"expected": evidence["expected"], "embedded": evidence["embedded"],
                "isbn": {"isbns": ["9780316085045"], "expectedMatch": False, "otherOwners": [{"bookId": 2}]},
                "content": {"expected_signal": {"ocr": True, "title_found": False, "title_near": False,
                                                "strong_identity": False},
                            "embedded_signal": {"ocr": True, "strong_identity": True}}}
    assert second_pass({"book_id": 4, "series": []}, evidence) == ("WRONG_CONTENT", 95)


@pytest.mark.parametrize("cover", [
    "JOHN SCALZI\nZOE'S TALE\n\nNEW YORK TIMES BESTSELLING\nAUTHOR OF\nTHE LAST COLONY",
    "STEPHENIE MEYER\nNEW MOON\nTHE SEQUEL TO\nTWILIGHT",
    "CHRISTOPHER PAOLINI\nELDEST\nFROM THE AUTHOR OF\nERAGON",
    "ZOE'S TALE\nJOHN SCALZI\nTHE COMPANION TO\nTHE LAST COLONY",
    "READ AN EXCERPT FROM\nTHE LAST COLONY\nBY JOHN SCALZI",
    "OLD MAN'S WAR\nTHE GHOST BRIGADES\nTHE LAST COLONY\nJOHN SCALZI\nTHREE NOVELS IN ONE",
    "A TOR BOOKS SAMPLER\nTHE LAST COLONY\nBY JOHN SCALZI",
    "SUPERSUMMARY\nSTUDY GUIDE\nTHE LAST COLONY\nJOHN SCALZI",
    "THE LAST COLONY\nA SUMMARY AND ANALYSIS\nJOHN SCALZI",
    "THE LAST COLONY\nTHE COMPLETE COLLECTION\nJOHN SCALZI",
])
def test_a_cover_that_refers_to_another_work_is_not_read_as_this_books_title(cover):
    assert _signal("The Last Colony", "John Scalzi", cover)["title_found"] is False
    for metadata in ({"title": "The Last Colony", "author": "John Scalzi"}, {"title": "", "author": ""}):
        verdict, _, _ = _scan("The Last Colony", "John Scalzi", cover,
                              file_title=metadata["title"], file_author=metadata["author"])
        assert verdict not in ("VERIFIED_CORRECT", "METADATA_ERROR"), (cover, metadata, verdict)


@pytest.mark.parametrize("credit", [
    "THE LAST COLONY\nA NOVEL\nBY JANE DOE\n\nINTRODUCTION BY\nJOHN SCALZI",
    "THE LAST COLONY\nJANE DOE\nEDITED BY\nJOHN SCALZI",
    "THE LAST COLONY\nJANE DOE\nFOREWORD BY\nJOHN SCALZI",
    "THE LAST COLONY\nJANE DOE\nTRANSLATED BY\nJOHN SCALZI",
    "THE LAST COLONY\nJANE DOE\nREAD BY\nJOHN SCALZI",
])
def test_a_role_given_on_the_line_above_the_name_is_not_the_authors_credit(credit):
    assert _signal("The Last Colony", "John Scalzi", credit)["author_found"] is False
    # ...while "by" or "a novel by" on its own line above the name still is the credit.
    assert _signal("The Last Colony", "John Scalzi", "THE LAST COLONY\nA NOVEL BY\nJOHN SCALZI")["author_found"]
    assert _signal("The Last Colony", "John Scalzi", "THE LAST COLONY\nBY\nJOHN SCALZI")["author_found"]


def test_the_title_and_author_must_be_on_one_page_even_to_get_the_lower_confidences(scalzi_bindery):
    """A half-title of this book followed by another book's title page (a merged PDF) verifies nothing."""
    pages = "THE LAST COLONY\fZOE'S TALE\nJOHN SCALZI\nA TOR BOOK\fFor my family"
    result = {"title": "The Last Colony", "author": "John Scalzi", "book_id": 3, "series": [], "isbn_evidence": {},
              "_ocr_text": True}
    for metadata in ({"title": "The Last Colony", "author": "John Scalzi"}, {"title": "The Last Colony", "author": "J. Scalzi"},
                     {"title": "The Last Colony - John Scalzi", "author": "John Scalzi"}, {"title": "", "author": ""}):
        verdict, confidence, _ = classify_identity(result, metadata, pages, [], [], pages)
        assert verdict == "INSUFFICIENT_EVIDENCE", (metadata, verdict, confidence)


def test_a_first_page_with_next_to_no_words_is_skipped_not_taken_as_the_opening():
    for noise in ("wo", "~", "i", "|  .", "—"):
        pages = f"{noise}\fTHE LAST COLONY\nJOHN SCALZI\nA TOR BOOK"
        assert _signal("The Last Colony", "John Scalzi", pages)["strong_identity"], noise


# --- ISBNs printed in a scan, and what the panel shows -----------------------------------------------

def _isbn13(body12: str) -> str:
    check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(body12)) % 10) % 10
    return body12 + str(check)


LAST_COLONY_ISBN = _isbn13("978076535" + "375")           # a checksum-valid ISBN-13
OTHER_ISBN = _isbn13("978076535" + "355")


def test_isbns_are_read_from_a_copyright_page_and_only_when_the_checksum_holds():
    from app.isbn_evidence import text_isbns
    page = f"Copyright 2007 by John Scalzi\nISBN {LAST_COLONY_ISBN[:3]}-{LAST_COLONY_ISBN[3]}-{LAST_COLONY_ISBN[4:9]}-{LAST_COLONY_ISBN[9:12]}-{LAST_COLONY_ISBN[12]}\n1 3 5 7 9 10 8 6 4 2"
    assert text_isbns(page) == [LAST_COLONY_ISBN]
    assert text_isbns(f"ISBN: {LAST_COLONY_ISBN}") == [LAST_COLONY_ISBN]
    assert text_isbns(f"ISBN-13: {LAST_COLONY_ISBN} (hardback)") == [LAST_COLONY_ISBN]
    assert text_isbns(f"First published {LAST_COLONY_ISBN[:3]} {LAST_COLONY_ISBN[3:]}") == [LAST_COLONY_ISBN]  # unlabelled 13
    assert text_isbns("ISBN 978-0-7653-5375-0") == []            # wrong check digit (an OCR misread): lost, not invented
    assert text_isbns("Printed in 2007, 1234567890123 copies, page 12345") == []
    assert text_isbns("") == [] and text_isbns("ISBN") == []
    # A 13-digit number whose checksum fails is a misread, never "really" a truncated ISBN-10.
    assert text_isbns("ISBN-13: 9780000003004") == [] and text_isbns("ISBN 9780000003004") == []
    assert text_isbns("ISBN 978076535375") == []                   # a digit lost: 12 digits is not an ISBN
    # A labelled ISBN-10 (with its X check character) is read as an ISBN-10.
    assert text_isbns("ISBN 0-7653-5375-X") == ["9780765353757"] == text_isbns("ISBN-10: 076535375X")
    assert text_isbns("ISBN-10: 0765353750") == []


@pytest.fixture
def scalzi_editions(scalzi_bindery):
    import sqlite3
    with sqlite3.connect(scalzi_bindery) as conn:
        conn.executescript(
            f"""
            CREATE TABLE editions (id INTEGER PRIMARY KEY, book_id INTEGER, isbn_13 TEXT, isbn_10 TEXT);
            INSERT INTO editions(book_id, isbn_13) VALUES (3, '{LAST_COLONY_ISBN}'), (2, '{OTHER_ISBN}');
            """
        )
    return scalzi_bindery


def _isbn_result(book_id=3, title="The Last Colony"):
    from app.isbn_evidence import isbn_evidence
    return lambda isbns: {"title": title, "author": "John Scalzi", "book_id": book_id, "series": [],
                          "isbn_evidence": isbn_evidence(book_id, isbns), "_ocr_text": True}


def test_a_printed_isbn_plus_the_download_record_verify_a_scan_whose_cover_cannot_be_read(scalzi_editions):
    """The case that motivated this: a children's-book scan whose cover and first pages OCR as noise."""
    result = _isbn_result()([f"isbn:{LAST_COLONY_ISBN}"])
    noise = "- +\nrise\fWARNING\nAmy relakyorwshep bo\fABOUT THE HERO\nA very ordinary boy, finding it hard to be a hero."
    verdict, confidence, evidence = classify_identity(result, {"title": "Book to Do 1", "author": "Unknown"},
                                                      noise, [f"isbn:{LAST_COLONY_ISBN}"], [], noise)
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 95)
    assert evidence["proof"]["sources"] == ["download name in Bindery's history",
                                            "the ISBN printed in the scan is one of this book's editions in Bindery"]


def test_a_printed_isbn_alone_or_naming_another_book_verifies_nothing(scalzi_editions):
    noise = "- +\nrise\fWARNING\nAmy relakyorwshep bo"
    junk = {"title": "Book to Do 1", "author": "Unknown"}
    # No download record for this book (book 2 has none): the ISBN is the only source.
    result = _isbn_result(book_id=2, title="Old Man's War")([f"isbn:{OTHER_ISBN}"])
    verdict, _, evidence = classify_identity(result, junk, noise, [f"isbn:{OTHER_ISBN}"], [], noise)
    assert verdict == "INSUFFICIENT_EVIDENCE" and "proof" not in evidence
    # The ISBN printed in the scan belongs to another Bindery book.
    result = _isbn_result()([f"isbn:{OTHER_ISBN}"])
    verdict, _, evidence = classify_identity(result, junk, noise, [f"isbn:{OTHER_ISBN}"], [], noise)
    assert verdict == "INSUFFICIENT_EVIDENCE" and evidence["isbn"]["otherOwners"][0]["bookId"] == 2


def test_a_printed_isbn_plus_the_title_page_verify_a_scan_without_a_download_record(scalzi_editions):
    result = _isbn_result(book_id=3)([f"isbn:{LAST_COLONY_ISBN}"])
    result["title"] = "The Last Colony"
    import sqlite3
    with sqlite3.connect(scalzi_editions) as conn:
        conn.execute("DELETE FROM history")
    page = "THE LAST COLONY\nJOHN SCALZI\nA TOR BOOK"
    verdict, confidence, evidence = classify_identity(result, {"title": "", "author": ""}, page,
                                                      [f"isbn:{LAST_COLONY_ISBN}"], [], page)
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 95)
    assert evidence["proof"]["sources"][1] == "the scan's title page"


@NEEDS_TOOLS
def test_a_real_scan_with_noise_pages_then_a_copyright_page_gets_its_isbn_read(tmp_path):
    pdf = scan_pdf(tmp_path / "scan.pdf", [
        ["WARNING", "THIS BOOK CONTAINS", "DRAGONS"],
        ["ABOUT HICCUP", "A very ordinary boy"],
        ["Copyright 2007 by John Scalzi", f"ISBN {LAST_COLONY_ISBN}", "1 3 5 7 9 10 8 6 4 2"],
    ], metadata={"title": "Book to Do 1", "author": "Unknown"})
    extracted = ebook_extraction.extract_ebook_identity(str(pdf))
    assert extracted.source == "native-pdf+ocr"
    assert extracted.identifiers == [f"isbn:{LAST_COLONY_ISBN}"] and extracted.ocr["isbns"] == [LAST_COLONY_ISBN]
    assert extracted.ocr["firstPage"].startswith("WARNING") and extracted.ocr["version"] >= 2


def test_a_printed_isbn_plus_the_files_own_matching_title_is_the_two_source_rule_ebooks_already_use(scalzi_editions):
    """ISBN (content) + the file's own title (metadata): two independent sources, verified at 99 as for an EPUB.

    A sibling's scan prints the sibling's ISBN, which Bindery maps to the sibling, so a cover cannot supply it."""
    import sqlite3
    with sqlite3.connect(scalzi_editions) as conn:
        conn.execute("DELETE FROM history")             # no download record, and the cover is unreadable
    noise = "- +\nrise\fWARNING\nAmy relakyorwshep bo"
    result = _isbn_result()([f"isbn:{LAST_COLONY_ISBN}"])
    verdict, confidence, evidence = classify_identity(
        result, {"title": "The Last Colony", "author": "John Scalzi"}, noise, [f"isbn:{LAST_COLONY_ISBN}"], [], noise)
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 99) and "ISBN is one of this book's editions" in evidence["explanation"]
    # ...while junk details and no other source leave the same scan undecided, and another book's ISBN is no help.
    verdict, _, _ = classify_identity(result, {"title": "Book to Do 1", "author": "Unknown"}, noise,
                                      [f"isbn:{LAST_COLONY_ISBN}"], [], noise)
    assert verdict == "INSUFFICIENT_EVIDENCE"
    other = _isbn_result()([f"isbn:{OTHER_ISBN}"])
    assert classify_identity(other, {"title": "The Last Colony", "author": "John Scalzi"}, noise,
                             [f"isbn:{OTHER_ISBN}"], [], noise)[0] == "INSUFFICIENT_EVIDENCE"


# --- the real "How to Steal a Dragon's Sword" scan: its title page is page 7 ------------------------------

DRAGON_SCAN = "\f".join([
    "- +\nrise",
    "[Baad\nSARNING\nAmy. relakyorwshep bo\nang Wualereak fact\nWHATSOEVER 4\nov HAVE BEEN\nWARNED",
    "ABOUT HICCUP\n\nHiccup Horrendous Haddock the Third was\nan awesome swordfighter, a dragon-whisperer,\n"
    "and the greatest Viking Hero that ever lived.\nBut Hiccup\u2019s memoirs look back to when\n"
    "he was a very ordinary boy, and finding\nit hard to be a Hero.",
    "the Witch Excellmeor\nahd\nScary with <-",
    "Ss athe;\ncork ss Hool; an",
    "ag Tn\n(aE ohn Aten =\nStaves\nThis book is a work of fiction. Names, characters, places, me incicicmia aire the\n"
    "ptexhict of the wihor\u2019s imagination or are t\nactual events, locales, or persons, li",
    "How to Steala\nDragon\u2019s Sword\nThe Heroic Misadventures of\nHiccup the Viking\nas told to\nCressida Cowell\n"
    "@\u00ae\u00a9\nLITTLE, BROWN AND COMPANY\nNew York Boston",
    "A small plea to readers:\nPLEASE DO NOT BLAME THE STORY\nUp anti now Hrecup bas just been playing at adventure,",
])


@pytest.fixture
def dragon_bindery(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "bindery.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER, excluded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, format TEXT, path TEXT, size_bytes INTEGER);
            CREATE TABLE history (id INTEGER PRIMARY KEY, book_id INTEGER, event_type TEXT, source_title TEXT, data TEXT, created_at TEXT);
            INSERT INTO authors VALUES (1, 'Cressida Cowell');
            INSERT INTO books(id, title, author_id) VALUES (2570, 'How to Steal a Dragon''s Sword', 1);
            INSERT INTO history(book_id, event_type, source_title) VALUES
              (2570, 'bookImported', 'Cressida Cowell - [How to Train Your Dragon 09] - How to Steal a Dragons Sword (image pdf)');
            """
        )
    monkeypatch.setattr(settings, "bindery_db", str(path))
    return path


def test_the_real_dragon_scan_is_verified_by_its_title_page_and_binderys_download_record(dragon_bindery):
    result = {"title": "How to Steal a Dragon's Sword", "author": "Cressida Cowell", "book_id": 2570,
              "series": [], "isbn_evidence": {}, "_ocr_text": True}
    junk = {"title": "Book to Do 1", "author": "Unknown"}
    verdict, confidence, evidence = classify_identity(result, junk, DRAGON_SCAN, [], [], DRAGON_SCAN)
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 90)
    assert evidence["proof"]["sources"] == ["download name in Bindery's history", "the scan's title page"]
    signal = evidence["content"]["expected_signal"]
    assert signal["strong_identity"] is True and signal["title_found"] and signal["author_found"]


def test_without_a_download_record_the_real_dragon_scan_stays_undecided_and_says_what_it_read(dragon_bindery):
    import sqlite3
    with sqlite3.connect(dragon_bindery) as conn:
        conn.execute("DELETE FROM history")
    result = {"title": "How to Steal a Dragon's Sword", "author": "Cressida Cowell", "book_id": 2570,
              "series": [], "isbn_evidence": {}, "_ocr_text": True}
    verdict, _, evidence = classify_identity(result, {"title": "Book to Do 1", "author": "Unknown"},
                                             DRAGON_SCAN, [], [], DRAGON_SCAN)
    assert verdict == "INSUFFICIENT_EVIDENCE" and evidence["scanPage"] == {"readsAs": "expected"}


@NEEDS_TOOLS
def test_a_real_scan_whose_title_page_is_page_seven_is_read_with_the_default_pages(tmp_path):
    assert settings.verification_ocr_pages >= 8
    noise = [["WARNING", "THIS BOOK CONTAINS", "DRAGONS"]] * 6
    title_page = ["HOW TO STEAL A", "DRAGON'S SWORD", "The Heroic Misadventures of", "as told to", "CRESSIDA COWELL"]
    pdf = scan_pdf(tmp_path / "scan.pdf", [*noise, title_page], metadata={"title": "Book to Do 1", "author": "Unknown"})
    verdict, _, evidence = _classify_scan(pdf, "How to Steal a Dragon's Sword", "Cressida Cowell")
    assert evidence["content"]["expected_signal"]["strong_identity"] is True
    assert verdict == "INSUFFICIENT_EVIDENCE" and evidence["scanPage"] == {"readsAs": "expected"}   # one source
