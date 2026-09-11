from app.automatic import _source_grab_event
from app.bindery_client import evaluate_replacement_candidate


def test_source_grab_requires_exact_import_grab_title_pair():
    items = [
        {"id": 20, "eventType": "bookImported", "sourceTitle": "Cat Hickey - Death of a Texan (epub)"},
        {"id": 19, "eventType": "grabbed", "sourceTitle": "Cat Hickey - Death of a Texan (epub)"},
        {"id": 18, "eventType": "grabbed", "sourceTitle": "Unrelated release"},
    ]
    event = _source_grab_event(items)
    assert event is not None
    assert event["id"] == 19


def test_source_grab_refuses_to_guess_without_pair():
    items = [
        {"id": 20, "eventType": "bookImported", "sourceTitle": "Imported release"},
        {"id": 19, "eventType": "grabbed", "sourceTitle": "Different grab"},
    ]
    assert _source_grab_event(items) is None


def test_automatic_candidate_requires_expected_author():
    result = {
        "approved": True,
        "title": "Cat of Death! retail epub",
    }
    decision = evaluate_replacement_candidate(
        result,
        expected_title="Cat of Death!",
        expected_author="",
    )
    assert decision.safe is False
    assert "author" in decision.reason.lower()


def test_same_wrong_release_on_different_indexer_stays_blocked_by_bookguard_gate():
    first = {
        "approved": False,
        "rejection": "release is blocklisted",
        "guid": "althub-guid",
        "title": "Cat Hickey - Death of a Texan (epub)",
    }
    second = {
        "approved": True,
        "guid": "nzbfinder-different-guid",
        "title": "Cat Hickey - Death of a Texan (epub)",
    }

    first_decision = evaluate_replacement_candidate(
        first,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )
    second_decision = evaluate_replacement_candidate(
        second,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )

    assert first_decision.safe is False
    assert second_decision.safe is False
