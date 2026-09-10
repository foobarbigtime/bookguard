import app.verifier_v048 as verifier


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
    return verifier._classify_identity(
        result,
        metadata,
        text,
        [],
        "test",
        [],
        front if front is not None else text[: verifier.FRONT_TEXT_CHARS],
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


def test_expected_identity_near_front_allows_metadata_error():
    text = "Cat of Death! by Aaron Blabey. Written and illustrated by Aaron Blabey. " + ("story " * 10000)
    verdict, confidence, evidence = classify(
        sample_result(),
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        text,
    )
    assert verdict == "METADATA_ERROR"
    assert confidence == 97
    assert evidence["content"]["expected_signal"]["strong_identity"] is True


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
    assert evidence["content"]["expected_signal"]["strong_identity"] is False


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
    assert evidence["content"]["expected_signal"]["strong_identity"] is True
    assert evidence["content"]["embedded_signal"]["strong_identity"] is True


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
