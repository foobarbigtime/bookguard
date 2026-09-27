"""Guarded post-quarantine grab and interrupted outcome proof."""

import sqlite3

import pytest

from app import automatic_quarantine_grab as grab
from app.automatic_contracts import AutomaticPostEffectUncertain
from app.config import settings
from app.db import (
    create_ebook_acquisition, ebook_replacement_for_quarantine_plan, init_local_db,
)


@pytest.fixture
def ready_grab(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    init_local_db()
    plan = {
        "id": 7, "signature": "quarantine-plan", "evidenceRevision": "revision",
        "planKind": "QUARANTINE_UNSAFE_MEDIA", "reasonCode": "UNSAFE_FILE",
        "subjectKind": "result", "subjectId": "4", "resultId": 4,
        "bookId": 101, "currentStep": 3,
    }
    step = {"code": "reacquire_expected_media", "externalMutation": True}
    result = {"id": 4, "scan_id": "scan", "book_id": 101}
    candidate = {
        "guid": "choice", "title": "Author - Book epub", "protocol": "usenet",
        "nzbUrl": "https://indexer.invalid/disposable.nzb", "size": 100,
    }
    fingerprint = grab._candidate_fingerprint(candidate)
    choice = {
        "currentPlan": True, "planId": 7, "planSignature": "quarantine-plan",
        "evidenceRevision": "revision", "quarantineExecutionId": 9,
        "quarantineSha256": "a" * 64, "candidateFingerprint": fingerprint,
        "candidate": {"guid": "choice", "title": candidate["title"],
                      "protocol": "usenet"},
    }

    class Client:
        queue = {"items": [], "partial": False}
        calls = 0

        def list_queue(self):
            return self.queue

        def grab(self, book_id, selected):
            self.calls += 1
            return {"queueItem": {"id": 77}}

    client = Client()
    monkeypatch.setattr(grab, "BinderyClient", lambda: client)
    monkeypatch.setattr(grab, "quarantine_selection_by_result", lambda result_id: choice)
    monkeypatch.setattr(grab, "quarantine_candidate_preview", lambda plan_id, guid, client: {
        "candidateFingerprint": fingerprint, "resultId": 4, "bookId": 101,
        "planSignature": "quarantine-plan",
    })
    monkeypatch.setattr(grab, "result_by_id", lambda result_id: result)
    monkeypatch.setattr(grab.workflow, "_result_and_book",
                        lambda result, client: ({}, "Book", "Author"))
    monkeypatch.setattr(grab.workflow, "_search_candidate",
                        lambda client, book_id, guid, title, author: candidate)
    monkeypatch.setattr(grab.workflow, "acquisition_readiness",
                        lambda client: {"ready": True, "blockers": []})
    return plan, step, choice, candidate, client


def _queue(candidate):
    return {"items": [{
        "id": 77, "bookId": 101, "title": candidate["title"],
        "protocol": candidate["protocol"], "status": "downloading",
    }], "partial": False}


def test_one_linked_grab_requires_queue_proof_and_blocks_second_child(ready_grab):
    plan, step, _, candidate, client = ready_grab
    boundary = grab._EXECUTOR.revalidate(plan, step)
    client.queue = _queue(candidate)
    result = grab._EXECUTOR.execute(plan, step, boundary)
    child = ebook_replacement_for_quarantine_plan(7)
    assert result["externalMutationPerformed"] is True
    assert result["admissionAttempted"] is False
    assert child["status"] == "queued" and child["queue_id"] == 77
    assert child["admission_id"] is None and client.calls == 1
    with pytest.raises(sqlite3.IntegrityError):
        create_ebook_acquisition(
            {"id": 4, "scan_id": "scan", "book_id": 101}, candidate,
            replacement_for_quarantine_plan_id=7,
        )
    with pytest.raises(grab.core.AutomaticExecutionBlocked,
                       match="linked replacement already exists"):
        grab._EXECUTOR.revalidate(plan, step)
    assert client.calls == 1


def test_uncertain_post_blocks_replay_then_adopts_exact_queue(ready_grab, monkeypatch):
    plan, step, choice, candidate, client = ready_grab
    boundary = grab._EXECUTOR.revalidate(plan, step)
    with pytest.raises(AutomaticPostEffectUncertain):
        grab._EXECUTOR.execute(plan, step, boundary)
    child = ebook_replacement_for_quarantine_plan(7)
    assert child["status"] == "grab_requested" and client.calls == 1
    existing = {"attemptCount": 1, "boundary": boundary}
    with pytest.raises(grab.core.AutomaticExecutionBlocked,
                       match="Exactly one current queue item"):
        grab._EXECUTOR.reconcile_uncertain(plan, step, existing)
    assert client.calls == 1
    client.queue = _queue(candidate)
    monkeypatch.setattr(grab.workflow, "reconcile_ebook_acquisition",
                        lambda child_id, supplied: {"acquisition": {
                            "queue_id": 77, "status": "queued",
                        }})
    adopted = grab._EXECUTOR.reconcile_uncertain(plan, step, existing)
    assert adopted["replacementAcquisitionId"] == child["id"]
    assert adopted["externalMutationPerformed"] is False
    assert client.calls == 1

    choice["candidateFingerprint"] = "changed"
    with pytest.raises(grab.core.AutomaticExecutionBlocked,
                       match="interrupted replacement child is unproven"):
        grab._EXECUTOR.reconcile_uncertain(plan, step, existing)
    assert client.calls == 1


def test_stale_custody_blocks_before_creating_child(ready_grab):
    plan, step, choice, _, client = ready_grab
    choice["currentPlan"] = False
    with pytest.raises(grab.core.AutomaticExecutionBlocked,
                       match="custody is unproven"):
        grab._EXECUTOR.revalidate(plan, step)
    assert ebook_replacement_for_quarantine_plan(7) is None
    assert client.calls == 0
