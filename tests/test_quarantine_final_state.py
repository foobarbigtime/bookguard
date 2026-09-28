from pathlib import Path

import pytest

from app import automatic_runner as runner
from app import quarantine_final_state as final_state
from app.config import settings
from app.file_safety import sha256_file


@pytest.fixture
def finalized_replacement(tmp_path, monkeypatch):
    retained = tmp_path / "quarantine" / "101" / "Unsafe.epub"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"unsafe original")
    published = tmp_path / "books" / "Unsafe.epub"
    published.parent.mkdir()
    published.write_bytes(b"verified replacement")
    monkeypatch.setattr(settings, "quarantine_root", str(retained.parent.parent))
    old_hash = sha256_file(retained)
    new_hash = sha256_file(published)
    plan = {
        "id": 7, "signature": "current", "evidenceRevision": "revision",
        "state": "ready", "currentStep": 5, "planKind": "QUARANTINE_UNSAFE_MEDIA",
        "reasonCode": "UNSAFE_FILE", "subjectKind": "result", "subjectId": "4",
        "resultId": 4, "bookId": 101, "path": "/data/books/Unsafe.epub",
        "steps": [{}, {}, {"code": "quarantine_exact_media"},
                  {"code": "reacquire_expected_media"},
                  {"code": "verify_replacement"},
                  {"code": "reconcile_final_state"}],
    }
    result = {
        "id": 4, "file_id": 9001, "book_id": 101, "scan_id": "scan",
        "stored_path": plan["path"], "local_path": str(published),
    }
    child = {
        "id": 8, "status": "finalized", "admission_id": 12,
        "result_id": 4, "book_id": 101, "scan_id": "scan", "queue_id": 77,
        "candidate_guid": "chosen", "staged_relative_path": "Replacement.epub",
        "staged_sha256": new_hash,
    }
    admission = {
        "id": 12, "status": "registered", "result_id": 4, "book_id": 101,
        "scan_id": "scan", "stored_path": result["stored_path"],
        "local_path": result["local_path"],
        "staged_relative_path": child["staged_relative_path"],
        "staged_sha256": new_hash,
    }
    receipt = {
        "id": 9, "state": "succeeded", "planId": 7, "attemptCount": 1,
        "boundary": {
            "expectedSha256": old_hash, "fileId": 9001, "bookId": 101,
            "storedPath": result["stored_path"], "localPath": result["local_path"],
        },
        "externalResult": {
            "status": "quarantined", "binderyDetached": True,
            "permanentDeletion": False, "replacementRequested": False,
            "resultId": 4, "fileId": 9001, "bookId": 101,
            "sha256": old_hash, "quarantinePath": str(retained),
        },
    }
    grab = {
        "state": "succeeded", "planId": 7, "attemptCount": 1,
        "boundary": {"candidateFingerprint": "fingerprint"},
        "externalResult": {
            "replacementAcquisitionId": 8, "queueId": 77,
            "admissionAttempted": False,
        },
    }
    selection = {
        "planId": 7, "planSignature": "current", "evidenceRevision": "revision",
        "quarantineExecutionId": 9, "quarantineSha256": old_hash,
        "candidateFingerprint": "fingerprint", "candidate": {"guid": "chosen"},
    }
    owners = [{"file_id": 9002, "book_id": 101}]
    queue = []
    monkeypatch.setattr(final_state, "recovery_plan_by_id", lambda _: plan)
    monkeypatch.setattr(final_state, "_current_plan", lambda _: plan)
    monkeypatch.setattr(final_state, "result_by_id", lambda _: result)
    monkeypatch.setattr(final_state, "ebook_replacement_for_quarantine_plan", lambda _: child)
    monkeypatch.setattr(final_state, "ebook_admission_by_id", lambda _: admission)
    monkeypatch.setattr(final_state, "quarantine_selection_by_result", lambda _: selection)
    monkeypatch.setattr(final_state.core, "_existing", lambda _, code, __: {
        "quarantine_exact_media": receipt, "reacquire_expected_media": grab,
    }.get(code))
    monkeypatch.setattr(final_state, "_verified_published_destination",
                        lambda admission, configured: Path(admission["local_path"])
                        if sha256_file(Path(admission["local_path"])) == new_hash
                        else (_ for _ in ()).throw(ValueError("published bytes changed")))
    monkeypatch.setattr(final_state, "staged_path_is_absent", lambda _: True)
    monkeypatch.setattr(final_state, "_queue_payload", lambda _: (queue, False))
    monkeypatch.setattr(final_state, "_exact_ebook_associations", lambda _: owners)
    monkeypatch.setattr(final_state, "bindery_file_by_id", lambda _: None)

    class Client:
        def get_book(self, book_id):
            return {"bookFiles": [{"format": "ebook", "path": plan["path"]}]}

    monkeypatch.setattr(final_state, "BinderyClient", Client)
    return plan, child, admission, retained, published, owners, queue


def test_final_state_records_only_local_completion(finalized_replacement, monkeypatch):
    plan, _, _, _, _, _, _ = finalized_replacement
    recorded = []
    monkeypatch.setattr(final_state.core, "record_recovery_step_success",
                        lambda plan_id, step: recorded.append((plan_id, step)) or {
                            **plan, "state": "completed", "currentStep": 6,
                        })
    proof = final_state.quarantine_final_state_preview(7)
    assert proof["safeToRecord"] is True
    outcome = final_state.run_quarantine_final_state_cycle(plan)
    assert outcome["state"] == "completed"
    assert outcome["externalMutationAttempted"] is False
    assert recorded == [(7, 5)]


def test_changed_retention_owner_bytes_or_child_refuses_completion(
    finalized_replacement, monkeypatch,
):
    plan, child, admission, retained, published, owners, queue = finalized_replacement
    for alter, restore in [
        (lambda: retained.write_bytes(b"tampered"), lambda: retained.write_bytes(b"unsafe original")),
        (lambda: owners[0].update(book_id=202), lambda: owners[0].update(book_id=101)),
        (lambda: published.write_bytes(b"tampered"), lambda: published.write_bytes(b"verified replacement")),
        (lambda: queue.append({"id": 77}), queue.clear),
        (lambda: child.update(status="admitted"), lambda: child.update(status="finalized")),
        (lambda: admission.update(staged_sha256="other"),
         lambda: admission.update(staged_sha256=child["staged_sha256"])),
    ]:
        alter()
        assert final_state.quarantine_final_state_preview(7)["safeToRecord"] is False
        restore()
    monkeypatch.setattr(final_state, "bindery_file_by_id",
                        lambda _: {"file_id": 9001, "book_id": 101})
    assert final_state.quarantine_final_state_preview(7)["safeToRecord"] is False
    child["status"] = "admitted"
    monkeypatch.setattr(final_state.core, "record_recovery_step_success",
                        lambda *_: pytest.fail("unproven state advanced"))
    monkeypatch.setattr(final_state.core, "block_recovery_plan",
                        lambda _, error: {**plan, "state": "blocked", "lastError": error})
    outcome = final_state.run_quarantine_final_state_cycle(plan)
    assert outcome["state"] == "blocked"
    assert outcome["externalMutationAttempted"] is False


def test_runner_waits_for_finalized_child_and_exact_allowlist(
    finalized_replacement, monkeypatch,
):
    plan, child, _, _, _, _, _ = finalized_replacement
    monkeypatch.setattr(runner.core, "recovery_plan_snapshot",
                        lambda limit: {"items": [plan]})
    monkeypatch.setattr(runner, "ebook_replacement_for_quarantine_plan", lambda _: child)
    monkeypatch.setattr(runner.core, "run_automatic_cycle",
                        lambda limit: pytest.fail("paused plan fell through"))
    monkeypatch.setattr(runner, "run_quarantine_final_state_cycle",
                        lambda _: {"state": "completed", "externalMutationAttempted": False})
    monkeypatch.setattr(runner.core, "paused_quarantine_result",
                        lambda _: {"state": "paused", "externalMutationAttempted": False})
    from types import SimpleNamespace
    configured = SimpleNamespace(automation_mode="automatic", automatic_action_allowlist=())
    monkeypatch.setattr(runner, "load_automation_settings", lambda: configured)
    assert runner.run_automatic_cycle()["state"] == "paused"
    configured.automatic_action_allowlist = ("reconcile_final_state",)
    child["status"] = "admitted"
    assert runner.run_automatic_cycle()["state"] == "paused"
    child["status"] = "finalized"
    assert runner.run_automatic_cycle()["state"] == "completed"
