"""Undecided books say what the file claims, what Bindery has, and what was found in the text."""

from __future__ import annotations

from app.library_review import review_item


def evidence(*, file_title="", file_author="", text=5000, title_in=False, author_in=False,
             file_title_in=False, extra=None):
    data = {
        "expected": {"title": "How to Steal a Dragon's Sword", "author": "Cressida Cowell"},
        "embedded": {"title": file_title, "author": file_author, "identifiers": []},
        "content": {
            "text_characters_examined": text,
            "expected_signal": {"title_found": title_in, "author_found": author_in},
            "embedded_signal": {"title_found": file_title_in, "author_found": False},
        },
        "explanation": "BookGuard could not obtain position-aware identity evidence strong enough for an automatic verdict.",
    }
    data.update(extra or {})
    return {"id": 1, "verdict": "INSUFFICIENT_EVIDENCE", "confidence": 40, "evidence": data}


ROW = {"id": 7, "book_id": 3, "title": "How to Steal a Dragon's Sword", "author": "Cressida Cowell", "format": "ebook"}


def why(row=None, **kwargs):
    item = review_item(dict(row or ROW), evidence(**kwargs))
    assert item["group"] == "undecided"
    return item["whyUndecided"]


def test_names_both_sides_and_missing_author():
    text = why(file_title="Dragon Sword", file_author="C. Cowell", title_in=True)
    assert "calls itself “Dragon Sword” by C. Cowell" in text
    assert "Bindery has it as “How to Steal a Dragon's Sword” by Cressida Cowell" in text
    assert "author “Cressida Cowell” does not" in text


def test_no_extractable_text_says_why_and_flags_placeholder_details():
    text = why({**ROW, "stored_path": "/b/a.epub"}, text=0, file_title="Book to Do 1", file_author="Unknown")
    assert "contains no readable text" in text and "look like placeholders" in text
    assert review_item({**ROW, "stored_path": "/b/a.epub"}, evidence(text=0, file_title="X"))["whyKind"] == "no_text"


def test_drm_unsupported_format_and_read_errors_are_told_apart():
    def kind_of(path, notes):
        ev = evidence(text=0, file_title="X", file_author="Y")
        ev["evidence"]["notes"] = notes
        return review_item({**ROW, "stored_path": path}, ev)["whyKind"]
    assert kind_of("/b/a.azw3", ["This Kindle book is DRM-protected, so its text cannot be read."]) == "drm"
    assert kind_of("/b/a.djvu", []) == "unsupported_format"
    assert kind_of("/b/a.epub", ["Native extraction error: bad zip"]) == "extract_error"


def test_audiobook_collection_needs_the_tags_to_name_several_works():
    row = {**ROW, "format": "audiobook", "title": "How To Train Your Dragon Collection: Books 4-6", "stored_path": "/a/Dragon"}
    ev = evidence(text=0)
    ev["evidence"]["expected"]["title"] = row["title"]
    ev["evidence"]["identity"] = {"detected_title": "Book Four", "detected_author": "Cressida Cowell",
                                  "wholeSet": {"mixedContent": True, "distinctMismatchTitleCount": 3,
                                               "titleMismatchCount": 30, "readableCount": 30}}
    item = review_item(row, ev)
    assert item["whyKind"] == "audiobook_collection" and "readable text" not in item["whyUndecided"]
    assert item["mixedAudio"] is True  # the recordings preview is offered


def test_a_single_book_with_a_collection_word_in_its_title_is_not_a_collection():
    # "The Complete Maus" with consistent tags: nothing says the folder holds several works.
    row = {**AUDIO_ROW, "title": "The Complete Maus", "stored_path": "/a/Maus"}
    ev = _audio_evidence("Maus", "Art Spiegelman", matches=0, mismatches=3, count=3)
    ev["evidence"]["expected"]["title"] = "The Complete Maus"
    item = review_item(row, ev)
    assert item["whyKind"] != "audiobook_collection" and item["mixedAudio"] is False



def test_breakdown_counts_reasons():
    from app.library_review import undecided_breakdown
    items = [{"group": "undecided", "whyKind": "drm"}, {"group": "undecided", "whyKind": "drm"},
             {"group": "undecided", "whyKind": "no_text"}, {"group": "wrong", "whyKind": ""}]
    assert undecided_breakdown(items) == [("DRM-protected", 2), ("No readable text", 1)]


def test_file_without_its_own_title():
    assert "carries no title or author of its own" in why(title_in=True, author_in=True)


def test_other_book_hint_when_only_the_files_title_is_in_the_text():
    assert "may be a different book" in why(file_title="Other Book", file_title_in=True)


def test_identity_is_exposed_and_unverified_items_have_none():
    item = review_item(dict(ROW), evidence(file_title="T", title_in=True))
    assert item["identity"]["expectedTitleInText"] is True
    assert item["identity"]["fileTitle"] == "T"
    bare = review_item(dict(ROW), None)
    assert bare["identity"] is None and bare["whyUndecided"] == ""


def test_volume_conflict_keeps_its_own_explanation():
    item = review_item(dict(ROW), evidence(extra={"reasonCode": "TITLE_DESIGNATION_CONFLICT",
                                                  "explanation": "Volume numbers disagree."}))
    assert item["whyUndecided"] == "Volume numbers disagree."


def test_collection_titles_from_real_libraries_are_recognised():
    from app.library_review import _COLLECTION_TITLE
    for title in ("How To Train Your Dragon - 10 Books", "How To Train Your Dragon Collection: Books 4-6",
                  "Harry Potter (series) 1-4", "The Complete Works", "Box Set"):
        assert _COLLECTION_TITLE.search(title) or "series" in title, title
    assert not _COLLECTION_TITLE.search("How to Steal a Dragon's Sword")


def _audio_evidence(detected_title, detected_author, *, matches=0, mismatches=0, names_strong=False, count=1):
    return {"id": 1, "verdict": "INSUFFICIENT_EVIDENCE", "confidence": 60, "evidence": {
        "expected": {"title": "Harry Potter en de Orde van de Feniks", "author": "J.K. Rowling", "mediaKind": "audiobook"},
        "identity": {"detected_title": detected_title, "detected_author": detected_author,
                     "wholeSet": {"titleMatchCount": matches, "titleMismatchCount": mismatches, "readableCount": count},
                     "filenameSupport": {"fileCount": count, "strong": names_strong}},
        "sampleFiles": [], "explanation": "The audiobook is technically readable, but not strong enough."}}


AUDIO_ROW = {**ROW, "format": "audiobook", "title": "Harry Potter en de Orde van de Feniks", "stored_path": "/a/HP"}


def test_audiobook_whose_tags_name_another_book_says_so():
    # Real library: tags "Harry Potter 03 - Gevangene van Azkaban" filed under "Orde van de Feniks".
    item = review_item(dict(AUDIO_ROW), _audio_evidence("Harry Potter 03 - Gevangene van Azkaban", "Jan Meng", mismatches=1))
    assert item["whyKind"] == "audiobook_other_work"
    assert "Gevangene van Azkaban" in item["whyUndecided"] and "different book" in item["whyUndecided"]
    assert item["identity"]["fileTitle"] == "Harry Potter 03 - Gevangene van Azkaban"
    assert item["identity"]["isAudiobook"] is True


def test_audiobook_without_tags_does_not_claim_the_tags_name_a_book():
    item = review_item(dict(AUDIO_ROW), _audio_evidence("", "", count=3))
    assert item["whyKind"] == "audiobook_no_tags"
    assert "no usable title tag" in item["whyUndecided"] and "filenames do not name the book" in item["whyUndecided"]


def test_retrieved_proof_is_exposed_for_the_panel():
    ev = evidence(title_in=True, author_in=True, file_title="T", file_author="A")
    ev["evidence"]["provenance"] = {"releases": [{"raw": "A - T (retail) (epub)"}], "notABook": ["VA-X-OST-WEB-2025"]}
    ev["evidence"]["proof"] = {"sources": ["download name in Bindery's history", "the book's text"]}
    who = review_item(dict(ROW), ev)["identity"]
    assert who["downloadNames"] == ["A - T (retail) (epub)"]
    assert who["notABook"] == ["VA-X-OST-WEB-2025"]
    assert who["proofSources"][0].startswith("download name")


def test_a_file_that_was_never_read_is_not_described_as_empty():
    ev = {"id": 1, "verdict": "INSUFFICIENT_EVIDENCE", "confidence": 0, "evidence": {
        "expected": {"title": "T", "author": "A"}, "embedded": {}, "content": {},
        "explanation": "Book identity could not be evaluated because no stable readable source snapshot was available."}}
    item = review_item({**ROW, "stored_path": "/b/T.epub"}, ev)
    assert item["whyKind"] == "not_checked"
    assert "no readable text" not in item["whyUndecided"] and "placeholders" not in item["whyUndecided"]
    assert "no stable readable source snapshot" in item["whyUndecided"]


def test_a_dotted_folder_name_is_not_a_file_type():
    ev = evidence(text=0, file_title="X", file_author="Y")
    item = review_item({**ROW, "stored_path": "/b/Mr. Mercedes"}, ev)
    assert "cannot read" not in item["whyUndecided"] and item["whyKind"] == "no_text"


def test_real_authors_and_matching_titles_are_not_called_placeholders():
    ev = evidence(text=0, file_title="", file_author="Daniel Silva")
    assert "placeholders" not in review_item({**ROW, "stored_path": "/b/a.epub"}, ev)["whyUndecided"]
    ev = evidence(text=0, file_title="How to Steal a Dragon's Sword", file_author="Cressida Cowell")
    assert "placeholders" not in review_item({**ROW, "stored_path": "/b/a.epub"}, ev)["whyUndecided"]


def test_breakdown_counts_unverified_and_scanner_outage_items_too():
    from app.library_review import undecided_breakdown
    items = [{"group": "undecided", "whyKind": "drm"}, {"group": "undecided", "whyKind": ""},
             {"group": "undecided", "whyKind": "", "scannerOutage": True}]
    labels = dict(undecided_breakdown(items))
    assert labels == {"DRM-protected": 1, "Not verified yet": 1, "Virus scan gave no result": 1}


def scan_why(*, extra=None, **kwargs):
    ocr = {"attempted": True, "pages": 3, "chars": 400, "error": "", "setAside": 1}
    item = review_item(dict(ROW), evidence(extra={"ocr": ocr, **(extra or {})}, **kwargs))
    return item["whyKind"], item["whyUndecided"]


def test_a_scan_whose_first_page_agrees_says_why_that_is_not_enough():
    kind, text = scan_why(extra={"scanPage": {"readsAs": "expected"}, "secondPass": [
        {"check": "scanProvenance", "releases": 0, "agreed": False}]})
    assert kind == "scan_page_only"
    assert "read the first 3 pages of this scan with OCR, setting aside 1 page that list other books" in text
    assert "reads as “How to Steal a Dragon's Sword” by Cressida Cowell" in text
    assert "only one source" in text and "carries no title or author" in text
    assert "Bindery has no download record for this book" in text
    _, text = scan_why(file_title="scan0001", file_author="Unknown",
                       extra={"scanPage": {"readsAs": "expected"}, "secondPass": [
                           {"check": "scanProvenance", "releases": 2, "agreed": False}]})
    assert "look like placeholders" in text and "does not name this exact title and author" in text


def test_a_scan_that_does_not_open_with_the_book_says_what_was_and_was_not_found():
    kind, text = scan_why(file_title="Zoe's Tale", file_author="John Scalzi")
    assert kind == "scan_not_opening" and "no page is a title page for “How to Steal a Dragon's Sword”" in text
    kind, text = scan_why(extra={"content": {
        "text_characters_examined": 400, "expected_signal": {"title_found": False, "title_near": True},
        "embedded_signal": {}}})
    assert kind == "scan_not_opening" and "may be an OCR misreading" in text
    kind, text = scan_why(extra={"content": {
        "text_characters_examined": 400, "expected_signal": {"title_found": True, "author_found": False},
        "embedded_signal": {}}})
    assert "author “Cressida Cowell” is not credited beside it" in text


def _isbn13(body12):
    check = (10 - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(body12)) % 10) % 10
    return body12 + str(check)


def test_the_scan_panel_shows_what_the_first_page_read_the_isbn_and_how_to_read_further():
    owned = _isbn13("978076535355")
    ocr = {"attempted": True, "pages": 3, "chars": 342, "error": "", "setAside": 0, "firstPage": "- + / rise"}
    kind, text = scan_why(file_title="Book to Do 1", file_author="Unknown", extra={"ocr": ocr})
    assert kind == "scan_not_opening" and "The first page reads: “- + / rise”." in text
    assert "No ISBN could be read from the 3 pages read" in text and "up to 12" in text
    # With an ISBN found, the hint about reading further goes and the ISBN's owner is named.
    isbn = {"isbns": [owned], "expectedMatch": False,
            "otherOwners": [{"bookId": 2, "title": "Old Man's War", "author": "John Scalzi"}]}
    kind, text = scan_why(file_title="Book to Do 1", extra={"ocr": {**ocr, "pages": 6, "isbns": [owned]}, "isbn": isbn})
    assert "belongs to “Old Man's War” by John Scalzi in Bindery, not to this book" in text and "No ISBN" not in text
    isbn["otherOwners"] = []
    kind, text = scan_why(extra={"ocr": {**ocr, "pages": 6, "isbns": [owned]}, "isbn": isbn})
    assert "is not one of this book's editions in Bindery" in text
