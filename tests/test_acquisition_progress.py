import pytest

from app import acquisition_progress as progress


def _plan():
    return {
        "id": 11, "planKind": "RECONCILE_ACQUISITION",
        "subjectKind": "acquisition", "subjectId": "4",
        "reasonCode": "ACQUISITION_PROGRESSABLE",
        "resultId": 7, "bookId": 9, "path": "old.epub",
        "signature": "current", "evidenceRevision": "revision",
        "currentStep": 0, "steps": [
            {"code": "reobserve_acquisition", "externalMutation": False},
            {"code": "resume_known_transition", "externalMutation": True},
        ],
    }


def _acquisition():
    return {
        "id": 4, "status": "queued", "result_id": 7, "book_id": 9,
        "queue_id": 23, "candidate_title": "Expected",
        "candidate_protocol": "torrent", "admission_id": None,
        "staged_sha256": None, "observed_relative_path": None,
    }


def _fixture(monkeypatch, *, queue_status="importexternal", items=None):
    acquisition = _acquisition()
    monkeypatch.setattr(progress, "ebook_acquisition_by_id", lambda _: acquisition)
    monkeypatch.setattr(progress.EXECUTOR, "_current_plan", lambda _: _plan())
    monkeypatch.setattr(progress, "BinderyClient", lambda: object())
    queue = [{"id": 23, "bookId": 9, "title": "Expected",
              "protocol": "torrent", "status": queue_status}]
    monkeypatch.setattr(progress, "_queue_payload", lambda _: (items if items is not None else queue, False))
    monkeypatch.setattr(progress, "list_staged_ebooks", lambda _: {
        "items": [{"relativePath": "book.epub", "size": 100, "modifiedNs": 123}],
        "truncated": False,
    })
    monkeypatch.setattr(progress, "resolve_staged_file", lambda _: (None, "staged"))
    monkeypatch.setattr(progress, "sha256_file", lambda _: "a" * 64)
    return acquisition


def test_known_handoff_requires_exact_queue_and_single_staged_file(monkeypatch):
    _fixture(monkeypatch)
    boundary = progress.EXECUTOR.revalidate(_plan(), _plan()["steps"][0])
    assert boundary["ok"] is True
    assert boundary["queueId"] == 23
    assert boundary["stagedSha256"] == "a" * 64


def test_foreign_queue_is_refused_without_progress(monkeypatch):
    _fixture(monkeypatch, items=[
        {"id": 23, "bookId": 999, "title": "Expected",
         "protocol": "torrent", "status": "importexternal"},
    ])
    boundary = progress.EXECUTOR.revalidate(_plan(), _plan()["steps"][0])
    assert boundary["ok"] is False
    assert "EXACT_QUEUE_IDENTITY" in [
        check["code"] for check in boundary["checks"] if not check["ok"]
    ]


def test_competing_queue_for_same_book_is_refused(monkeypatch):
    _fixture(monkeypatch, items=[
        {"id": 23, "bookId": 9, "title": "Expected",
         "protocol": "torrent", "status": "importexternal"},
        {"id": 24, "bookId": 9, "title": "Another",
         "protocol": "torrent", "status": "downloading"},
    ])
    boundary = progress.EXECUTOR.revalidate(_plan(), _plan()["steps"][0])
    assert "NO_COMPETING_QUEUE" in [
        check["code"] for check in boundary["checks"] if not check["ok"]
    ]


def test_incomplete_handoff_waits_without_journaling(monkeypatch):
    _fixture(monkeypatch, queue_status="downloading")
    monkeypatch.setattr(progress.core, "recovery_plan_by_id", lambda _: _plan())
    monkeypatch.setattr(progress.core, "attempt_automatic_step", lambda _: pytest.fail("must not execute"))
    monkeypatch.setattr(progress.core, "record_recovery_step_success", lambda *_: pytest.fail("must not advance"))
    response = progress.run_acquisition_progress_cycle(_plan())
    assert response["state"] == "waiting"
    assert response["externalMutationAttempted"] is False


def test_execute_passes_exact_queue_and_fingerprint_to_guarded_primitive(monkeypatch):
    acquisition = _fixture(monkeypatch)
    boundary = progress.EXECUTOR.revalidate(_plan(), _plan()["steps"][1])
    calls = []

    def reconcile(acquisition_id, *, expected_queue_id, expected_staged_fingerprint):
        calls.append((acquisition_id, expected_queue_id, expected_staged_fingerprint))
        return {"ok": True, "acquisition": {
            **acquisition, "status": "staging_observed",
            "observed_relative_path": "book.epub",
            "observed_size": 100, "observed_modified_ns": 123,
        }}

    monkeypatch.setattr(progress, "reconcile_ebook_acquisition", reconcile)
    response = progress.EXECUTOR.execute(_plan(), _plan()["steps"][1], boundary)
    assert calls == [(4, 23, ("book.epub", 100, 123))]
    assert response["admissionAttempted"] is False


def test_staged_observation_verifies_only_through_guarded_primitive(monkeypatch):
    acquisition = _fixture(monkeypatch)
    acquisition.update(status="staging_observed", observed_relative_path="book.epub",
                       observed_size=100, observed_modified_ns=123)
    boundary = progress.EXECUTOR.revalidate(_plan(), _plan()["steps"][1])
    monkeypatch.setattr(progress, "reconcile_ebook_acquisition", lambda *args, **kwargs: {
        "ok": True, "acquisition": {
            **acquisition, "status": "verified", "staged_relative_path": "book.epub",
            "staged_sha256": "a" * 64,
        },
    })
    response = progress.EXECUTOR.execute(_plan(), _plan()["steps"][1], boundary)
    assert response["status"] == "verified"
    assert response["admissionAttempted"] is False


def test_unproven_interrupted_progress_does_not_replay(monkeypatch):
    _fixture(monkeypatch)
    with pytest.raises(progress.core.AutomaticExecutionBlocked) as exc:
        progress.EXECUTOR.reconcile_uncertain(
            _plan(), _plan()["steps"][1],
            {"boundary": {"queueId": 23, "fingerprint": ["book.epub", 100, 123],
                          "stagedSha256": "a" * 64}},
        )
    assert exc.value.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME"


def test_proven_interrupted_observation_can_be_adopted(monkeypatch):
    acquisition = _fixture(monkeypatch)
    acquisition.update(status="staging_observed", observed_relative_path="book.epub",
                       observed_size=100, observed_modified_ns=123)
    result = progress.EXECUTOR.reconcile_uncertain(
        _plan(), _plan()["steps"][1],
        {"boundary": {"queueId": 23, "fingerprint": ["book.epub", 100, 123],
                      "stagedSha256": "a" * 64}},
    )
    assert result["reconciledAfterRestart"] is True
    assert result["externalMutationPerformed"] is False
