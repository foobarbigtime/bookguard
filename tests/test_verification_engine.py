import app.verification_engine as verifier
from app.verification_constants import FRONT_TEXT_CHARS


# These tests pin the position-aware identity classifier and its safety rules.


def sample_result(title="Cat of Death!", author="Aaron Blabey"):
    return {
        "id": 1,
        "scan_id": "scan-1",
        "file_id": 10,
        "book_id": 20,
        "classification": "REVIEW",
        "reason_code": "MISMATCH",
        "format": "ebook",
        "author": author,
        "title": title,
        "stored_path": "/data/media/books/example.epub",
        "local_path": "/books/example.epub",
        "reasons": ["Embedded metadata does not match"],
        "metadata": {},
    }


def classify(result, metadata, text, front=None):
    return verifier.classify_identity(
        result,
        metadata,
        text,
        [],
        [],
        front if front is not None else text[:FRONT_TEXT_CHARS],
    )


def test_known_wrong_content_stays_wrong():
    text = "Death of a Texan. A novel by Cat Hickey. Copyright Cat Hickey. " * 20
    verdict, confidence, evidence = classify(
        sample_result(),
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        text,
    )
    assert verdict == "WRONG_CONTENT"
    assert confidence == 99
    assert evidence["content"]["embedded_signal"]["strong_identity"] is True


def test_clear_title_page_metadata_error_is_still_allowed():
    text = "Cat of Death! by Aaron Blabey. Written and illustrated by Aaron Blabey. " + ("story " * 10000)
    verdict, confidence, evidence = classify(
        sample_result(),
        {"title": "Totally Wrong Metadata", "author": "Someone Else"},
        text,
    )
    assert verdict == "METADATA_ERROR"
    assert confidence == 97
    assert evidence["content"]["expected_signal"]["front_proximity_chars"] <= 500


def test_backmatter_mentions_do_not_trigger_metadata_error():
    front = "A completely different novel by Jane Example. " + ("chapter text " * 5000)
    back = "Also by this publisher: Cat of Death! by Aaron Blabey."
    text = front + back
    verdict, confidence, evidence = classify(
        sample_result(),
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        text,
        front=front,
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence == 70
    assert evidence["content"]["expected_title_found"] is True


def test_mixed_frontmatter_stays_ambiguous():
    text = (
        "Cat of Death! by Aaron Blabey. "
        "This edition also contains Death of a Texan by Cat Hickey. "
        + ("story " * 10000)
    )
    verdict, confidence, evidence = classify(
        sample_result(),
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        text,
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence < 90


def test_matching_metadata_with_front_content_is_verified():
    result = sample_result()
    text = "Cat of Death! by Aaron Blabey. Written and illustrated by Aaron Blabey."
    verdict, confidence, evidence = classify(
        result,
        {"title": "Cat of Death!", "author": "Aaron Blabey"},
        text,
    )
    assert verdict == "VERIFIED_CORRECT"
    assert confidence == 99
    assert evidence["metadata_matches_expected"] is True


def test_catalog_article_and_generic_novel_suffix_are_identity_equivalent():
    result = sample_result(title="English Girl", author="Daniel Silva")
    text = "The English Girl: A Novel Daniel Silva " + ("chapter " * 100)

    verdict, confidence, evidence = classify(
        result,
        {"title": "The English Girl: A Novel", "author": "Daniel Silva"},
        text,
    )

    assert verdict == "VERIFIED_CORRECT"
    assert confidence == 99
    assert evidence["metadata_matches_expected"] is True
    assert evidence["content"]["expected_signal"]["strong_identity"] is True
    assert evidence["content"]["embedded_signal"]["strong_identity"] is True


def test_substantive_subtitle_is_not_discarded_as_catalogue_noise():
    result = sample_result(title="English Girl", author="Daniel Silva")
    text = "The English Girl: Spy Stories Daniel Silva " + ("chapter " * 100)

    verdict, confidence, evidence = classify(
        result,
        {"title": "The English Girl: Spy Stories", "author": "Daniel Silva"},
        text,
    )

    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence == 70
    assert evidence["metadata_matches_expected"] is False


def test_same_title_conflicting_author_is_never_auto_rewritten():
    result = sample_result(title="One Last Strike", author="John Grisham")
    text = "One Last Strike John Grisham " + ("chapter " * 1000)
    verdict, confidence, evidence = classify(
        result,
        {"title": "One Last Strike", "author": "Tony La Russa"},
        text,
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence == 70
    assert "author conflicts" in evidence["explanation"]


def test_conflicting_title_at_book_start_blocks_metadata_repair():
    result = sample_result(title="Wedding Florist", author="James Patterson")
    text = (
        "The Radcliffes "
        + ("publisher material " * 80)
        + "Wedding Florist by James Patterson "
        + ("story " * 1000)
    )
    verdict, confidence, evidence = classify(
        result,
        {"title": "The Radcliffes", "author": "T.J. Kline"},
        text,
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence == 70
    assert "very start" in evidence["explanation"]


def test_expected_title_author_too_far_apart_blocks_metadata_repair():
    result = sample_result(title="Return", author="James Patterson")
    text = "Return " + ("front matter " * 80) + "James Patterson " + ("story " * 1000)
    verdict, confidence, evidence = classify(
        result,
        {"title": "Another Book", "author": "Someone Else"},
        text,
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence == 70
    assert "not tightly enough" in evidence["explanation"]


def test_short_expected_title_inside_longer_embedded_title_is_not_independent_evidence():
    result = sample_result(title="Return", author="James Patterson")
    text = "The Return BookShots Flames by Erin Knightley. Return James Patterson. " + ("story " * 1000)
    verdict, confidence, evidence = classify(
        result,
        {"title": "The Return (BookShots Flames)", "author": "Erin Knightley"},
        text,
    )
    # Safety behavior is what matters here: this pattern must never authorize
    # an automatic metadata rewrite, regardless of which conservative rule
    # produces the INSUFFICIENT_EVIDENCE explanation first.
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence == 70


def test_collection_like_embedded_title_blocks_single_book_metadata_repair():
    result = sample_result(title="Siren Depths", author="Martha Wells")
    text = "Siren Depths by Martha Wells. " + ("story " * 1000)
    verdict, confidence, evidence = classify(
        result,
        {"title": "The Books of the Raksura: The Complete Raksura Series", "author": "Martha Wells"},
        text,
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence == 70
    assert "collection" in evidence["explanation"].lower() or "series" in evidence["explanation"].lower()


def test_non_collection_bad_metadata_can_still_be_repaired():
    result = sample_result(title="The Enchantment", author="Kristin Hannah")
    text = "The Enchantment by Kristin Hannah. " + ("story " * 1000)
    verdict, confidence, evidence = classify(
        result,
        {"title": "Wrong Metadata", "author": "Wrong Author"},
        text,
    )
    assert verdict == "METADATA_ERROR"
    assert confidence == 97


def test_missing_embedded_metadata_can_be_verified_for_repair():
    result = sample_result(title="Bel Canto", author="Ann Patchett")
    result["reason_code"] = "NO_METADATA"
    text = "Bel Canto by Ann Patchett. " + ("story " * 1000)

    verdict, confidence, evidence = classify(
        result,
        {},
        text,
    )

    assert verdict == "METADATA_ERROR"
    assert confidence == 97
    assert evidence["embedded"]["title"] == ""
    assert evidence["embedded"]["author"] == ""
    assert evidence["content"]["expected_signal"]["strong_identity"] is True
    assert evidence["content"]["expected_signal"]["front_proximity"] is True
