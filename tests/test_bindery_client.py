from app.bindery_client import evaluate_replacement_candidate


def test_duplicate_wrong_release_from_second_indexer_is_rejected():
    result = {
        "approved": True,
        "guid": "https://nzbfinder.example/different-guid",
        "title": "Cat Hickey - Death of a Texan (epub)",
        "author": "",
        "bookTitle": "",
    }

    decision = evaluate_replacement_candidate(
        result,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )

    assert decision.safe is False
    assert "author" in decision.reason.lower()


def test_matching_title_and_author_is_allowed():
    result = {
        "approved": True,
        "title": "Aaron Blabey - Cat of Death! (retail) (epub)",
        "author": "",
        "bookTitle": "",
    }

    decision = evaluate_replacement_candidate(
        result,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )

    assert decision.safe is True


def test_bindery_rejection_is_preserved():
    result = {
        "approved": False,
        "rejection": "release is blocklisted",
        "title": "Aaron Blabey - Cat of Death! (epub)",
    }

    decision = evaluate_replacement_candidate(
        result,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )

    assert decision.safe is False
    assert decision.reason == "release is blocklisted"
