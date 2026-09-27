import pytest

from app import quarantine_selection as selection
from app.acquisition import AcquisitionSafetyError
from app.config import settings
from app.db import init_local_db


@pytest.fixture
def choice_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    init_local_db()
    plan = {
        "id": 7, "signature": "exact-plan", "evidenceRevision": "current-evidence",
        "state": "ready", "currentStep": 3,
    }
    result = {"id": 4, "book_id": 101}
    candidate = {
        "guid": "new-guid", "title": "Fixture Author - Fixture Book epub",
        "nzbUrl": "https://indexer.invalid/disposable.nzb", "size": 120,
        "protocol": "usenet", "mediaType": "ebook", "indexerName": "Fixture",
    }
    receipt = {
        "id": 9, "state": "succeeded",
        "externalResult": {"sha256": "a" * 64},
    }
    monkeypatch.setattr(selection, "quarantine_replacement_preview",
                        lambda plan_id, client=None: {
                            "safeForCandidateReview": plan["currentStep"] == 3,
                            "resultId": 4,
                        })
    monkeypatch.setattr(selection, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(selection, "result_by_id", lambda result_id: result)
    monkeypatch.setattr(selection, "_result_and_book",
                        lambda result, client: ({}, "Fixture Book", "Fixture Author"))
    monkeypatch.setattr(selection, "_search_candidate",
                        lambda client, book_id, guid, title, author: candidate)
    monkeypatch.setattr(selection.core, "_existing", lambda *args: receipt)
    return plan, candidate, receipt


def test_quarantine_choice_is_durable_immutable_and_never_exposes_url(choice_setup):
    _, candidate, _ = choice_setup
    first = selection.bind_quarantine_candidate(7, "new-guid", client=object())
    again = selection.bind_quarantine_candidate(7, "new-guid", client=object())
    after_restart = selection.quarantine_selection_by_result(4)

    assert first == again == after_restart
    assert first["currentPlan"] is True
    assert first["liveGrabEnabled"] is False
    assert "nzbUrl" not in str(first)
    assert "disposable.nzb" not in str(first)
    assert len(first["candidateFingerprint"]) == 64

    candidate["nzbUrl"] = "https://indexer.invalid/changed.nzb"
    with pytest.raises(AcquisitionSafetyError, match="different or stale"):
        selection.bind_quarantine_candidate(7, "new-guid", client=object())
    assert selection.quarantine_selection_by_result(4)["candidateFingerprint"] == (
        first["candidateFingerprint"]
    )


def test_quarantine_choice_refuses_stale_custody_and_receipt(choice_setup):
    plan, _, receipt = choice_setup
    selected = selection.bind_quarantine_candidate(7, "new-guid", client=object())
    plan["currentStep"] = 4
    assert selection.quarantine_selection_by_result(4)["currentPlan"] is False
    with pytest.raises(AcquisitionSafetyError, match="custody"):
        selection.bind_quarantine_candidate(7, "new-guid", client=object())

    plan["currentStep"] = 3
    receipt["id"] = 10
    assert selection.quarantine_selection_by_result(4)["currentPlan"] is False
    with pytest.raises(AcquisitionSafetyError, match="different or stale"):
        selection.bind_quarantine_candidate(7, "new-guid", client=object())
    assert selected["quarantineExecutionId"] == 9
