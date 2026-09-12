from app.verification_engine import classify_identity


def sample_result():
    return {
        "id": 1,
        "scan_id": "scan-1",
        "file_id": 10,
        "book_id": 20,
        "classification": "REVIEW",
        "reason_code": "MISMATCH",
        "format": "ebook",
        "author": "Aaron Blabey",
        "title": "Cat of Death!",
        "stored_path": "/data/media/books/Aaron Blabey/Cat of Death.epub",
        "local_path": "/books/Aaron Blabey/Cat of Death.epub",
        "reasons": ["Embedded metadata does not match"],
        "metadata": {"title": "Death of a Texan", "author": "Cat Hickey"},
    }


def classify(metadata, text):
    verdict, confidence, evidence = classify_identity(
        sample_result(), metadata, text, [], []
    )
    return verdict, confidence, evidence


def test_wrong_content_requires_content_support_for_conflicting_identity():
    verdict, confidence, evidence = classify(
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        "Death of a Texan. A novel by Cat Hickey. Copyright Cat Hickey.",
    )
    assert verdict == "WRONG_CONTENT"
    assert confidence == 99
    assert evidence["content"]["embedded_title_found"] is True
    assert evidence["content"]["embedded_author_found"] is True
    assert evidence["content"]["expected_title_found"] is False
    assert evidence["content"]["expected_author_found"] is False


def test_metadata_error_requires_expected_title_and_author_in_content():
    verdict, confidence, evidence = classify(
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        "Cat of Death! by Aaron Blabey. Copyright Aaron Blabey.",
    )
    assert verdict == "METADATA_ERROR"
    assert confidence >= 90
    assert evidence["content"]["expected_title_found"] is True
    assert evidence["content"]["expected_author_found"] is True
    assert evidence["content"]["embedded_title_found"] is False
    assert evidence["content"]["embedded_author_found"] is False


def test_verified_correct_needs_matching_metadata_and_content():
    verdict, confidence, evidence = classify(
        {"title": "Cat of Death!", "author": "Aaron Blabey"},
        "Cat of Death! by Aaron Blabey. Written and illustrated by Aaron Blabey.",
    )
    assert verdict == "VERIFIED_CORRECT"
    assert confidence == 99
    assert evidence["metadata_matches_expected"] is True


def test_mixed_identity_stays_ambiguous():
    verdict, confidence, evidence = classify(
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        "Cat of Death! by Aaron Blabey. Also includes Death of a Texan by Cat Hickey.",
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence < 90
    assert evidence["content"]["expected_title_found"] is True
    assert evidence["content"]["embedded_title_found"] is True
