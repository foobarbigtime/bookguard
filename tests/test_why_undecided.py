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


def why(**kwargs):
    item = review_item(dict(ROW), evidence(**kwargs))
    assert item["group"] == "undecided"
    return item["whyUndecided"]


def test_names_both_sides_and_missing_author():
    text = why(file_title="Dragon Sword", file_author="C. Cowell", title_in=True)
    assert "calls itself “Dragon Sword” by C. Cowell" in text
    assert "Bindery has it as “How to Steal a Dragon's Sword” by Cressida Cowell" in text
    assert "author “Cressida Cowell” does not" in text


def test_no_extractable_text_is_called_out():
    assert "No readable text could be extracted" in why(text=0, file_title="X")


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
