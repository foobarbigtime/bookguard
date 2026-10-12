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


def test_audiobook_collection_is_not_called_unreadable_text():
    row = {**ROW, "format": "audiobook", "title": "How To Train Your Dragon - 10 Books", "stored_path": "/a/Dragon"}
    ev = evidence(text=0)
    ev["evidence"]["expected"]["title"] = "How To Train Your Dragon - 10 Books"
    item = review_item(row, ev)
    assert item["whyKind"] == "audiobook_collection" and "readable text" not in item["whyUndecided"]
    assert item["mixedAudio"] is True  # the recordings preview is offered


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
