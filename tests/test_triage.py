from copy import deepcopy

import app.triage as triage


def sample_result():
    return {
        "id": 10,
        "scan_id": "scan-a",
        "file_id": 55,
        "book_id": 77,
        "classification": "REVIEW",
        "reason_code": "MISMATCH",
        "format": "ebook",
        "author": "Expected Author",
        "title": "Expected Title",
        "stored_path": "/data/media/books/Expected/Book.epub",
        "local_path": "/books/Expected/Book.epub",
        "reasons": ["Metadata does not match"],
        "metadata": {"title": "Other Title", "author": "Other Author"},
    }


def test_signature_survives_rescan_identity_only_changes():
    first = sample_result()
    second = deepcopy(first)
    second["id"] = 999
    second["scan_id"] = "scan-b"

    assert triage.result_signature(first) == triage.result_signature(second)


def test_signature_reopens_when_detected_metadata_changes():
    first = sample_result()
    second = deepcopy(first)
    second["metadata"]["title"] = "Corrected Title"

    assert triage.result_signature(first) != triage.result_signature(second)


def test_signature_reopens_when_reason_changes():
    first = sample_result()
    second = deepcopy(first)
    second["reason_code"] = "PARTIAL_MATCH"

    assert triage.result_signature(first) != triage.result_signature(second)


def test_triage_preview_separates_detach_and_quarantine(monkeypatch):
    item = sample_result()
    monkeypatch.setattr(triage, "_require_current_triage_result", lambda result: None)
    monkeypatch.setattr(triage, "_exact_bindery_match", lambda result: True)
    monkeypatch.setattr(triage.os.path, "exists", lambda path: True)
    monkeypatch.setattr(
        triage,
        "ebook_action_preview",
        lambda local_path, stored_path: {
            "ready": True,
            "checks": {},
            "blockers": [],
        },
    )

    detach = triage.triage_action_preview(item, "detach")
    quarantine = triage.triage_action_preview(item, "quarantine")

    assert detach["safe"] is True
    assert quarantine["safe"] is True
    assert detach["action"] == "detach"
    assert quarantine["action"] == "quarantine"
