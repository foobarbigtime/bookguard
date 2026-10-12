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


def test_ocr_text_with_a_lost_space_still_finds_the_title_and_author():
    from app.verification_engine import _identity_signal
    page = "HOW TO STEALA\nDRAGON'S SWORD\nCRESSIDA COWELL"
    signal = _identity_signal("How to Steal a Dragon's Sword", "Cressida Cowell", page, page, True)
    assert signal["title_found"] and signal["author_found"] and signal["strong_identity"]
    assert signal["ocrApproximate"] is False
    # Without the OCR flag the same text is (rightly) not an exact match.
    assert not _identity_signal("How to Steal a Dragon's Sword", "Cressida Cowell", page, page)["title_found"]


def test_a_misread_letter_is_found_but_never_strong_identity():
    from app.verification_engine import _identity_signal
    misread = "HOW TO STEAL A DRAGON'S SWORD CRESSIDA COWELI"
    signal = _identity_signal("How to Steal a Dragon's Sword", "Cressida Cowell", misread, misread, True)
    assert signal["author_found"] and signal["ocrApproximate"] is True
    assert signal["strong_identity"] is False       # only an exact title AND exact author can be strong


def test_a_similar_title_by_the_same_author_is_never_accepted_as_strong():
    from app.verification_engine import _identity_signal
    page = "THE LOST COLONY JOHN SCALZI"            # one letter from the expected "The Last Colony"
    signal = _identity_signal("The Last Colony", "John Scalzi", page, page, True)
    assert signal["strong_identity"] is False and signal["ocrApproximate"] is True
    result = {"title": "The Last Colony", "author": "John Scalzi", "book_id": 1, "series": [],
              "isbn_evidence": {}, "_ocr_text": True}
    verdict, confidence, _ = classify_identity(result, {"title": "", "author": ""}, page, [], [], page)
    assert (verdict, confidence) != ("METADATA_ERROR", 97) and verdict != "VERIFIED_CORRECT"


def test_ocr_tolerance_never_turns_one_book_into_another():
    from app.verification_engine import _identity_signal

    def found(title, page):
        return _identity_signal(title, "Cressida Cowell", page, page, True)["title_found"]

    page = "HOW TO CHEAT A DRAGON'S CURSE CRESSIDA COWELL"
    assert not found("How to Steal a Dragon's Sword", page)               # a different book by the same author
    assert not found("The 13-Storey Treehouse", "THE 26-STOREY TREEHOUSE")  # digits must match exactly
    assert not found("Kill", "SKILLED KILLERS")                            # short titles are never fuzzy
    assert not found("A Book Nobody Has", "SOMETHING ELSE ENTIRELY WRITTEN HERE")
    assert found("The 13-Storey Treehouse", "THE 13 STOREY TREEHOUSE")


def test_ocr_search_work_is_bounded_whatever_the_text_size(monkeypatch):
    """Deterministic: counts comparisons instead of timing them."""
    from app import verification_engine as engine
    calls = []
    real = engine.SequenceMatcher

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "SequenceMatcher", counting)
    hostile = ("hownotostealadragonsworx " * 40_000)          # 1,000,000 characters, nearly the title
    engine._ocr_find("howtostealadragonssword", engine._squash(hostile), engine.OCR_MIN_SQUASHED_TITLE)
    assert 0 < len(calls) <= engine.OCR_MAX_WINDOWS
    assert len(engine._squash(hostile)[:engine.OCR_MAX_CHARS]) == engine.OCR_MAX_CHARS
