from pathlib import Path

import pytest

from app import quarantine_handoff as handoff
from app.config import settings
from app.file_safety import sha256_file


@pytest.fixture
def verified_handoff(tmp_path, monkeypatch):
    root = tmp_path / "quarantine"
    retained = root / "101" / "Unsafe.epub"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"exact quarantined bytes")
    staged = tmp_path / "staging" / "Replacement.epub"
    staged.parent.mkdir()
    staged.write_bytes(b"exact verified replacement bytes")
    monkeypatch.setattr(settings, "quarantine_root", str(root))
    sha = sha256_file(retained)
    staged_sha = sha256_file(staged)
    fingerprint = (staged.name, staged.stat().st_size, staged.stat().st_mtime_ns)
    plan = {
        "id": 7, "signature": "current-plan", "evidenceRevision": "revision",
        "state": "ready", "currentStep": 4,
        "planKind": "QUARANTINE_UNSAFE_MEDIA", "reasonCode": "UNSAFE_FILE",
        "subjectKind": "result", "subjectId": "4", "resultId": 4,
        "bookId": 101, "path": "/books/Unsafe.epub",
        "steps": [
            {}, {}, {"code": "quarantine_exact_media"},
            {"code": "reacquire_expected_media"}, {"code": "verify_replacement"},
        ],
    }
    result = {
        "id": 4, "file_id": 9001, "scan_id": "scan", "book_id": 101,
        "local_path": str(tmp_path / "books" / "Unsafe.epub"),
        "stored_path": plan["path"],
    }
    selection = {
        "planId": 7, "planSignature": plan["signature"],
        "evidenceRevision": plan["evidenceRevision"],
        "quarantineExecutionId": 9, "quarantineSha256": sha,
        "candidateFingerprint": "fingerprint",
        "candidate": {"guid": "chosen", "title": "Author - Book epub",
                      "protocol": "usenet"},
    }
    child = {
        "id": 8, "status": "verified", "admission_id": None,
        "result_id": 4, "book_id": 101, "scan_id": "scan",
        "candidate_guid": "chosen", "candidate_title": "Author - Book epub",
        "candidate_protocol": "usenet", "queue_id": 77,
        "staged_relative_path": fingerprint[0], "staged_sha256": staged_sha,
        "observed_relative_path": fingerprint[0],
        "observed_size": fingerprint[1], "observed_modified_ns": fingerprint[2],
    }
    quarantine = {
        "id": 9, "planId": 7, "state": "succeeded", "attemptCount": 1,
        "boundary": {
            "expectedSha256": sha, "fileId": 9001, "bookId": 101,
            "storedPath": result["stored_path"], "localPath": result["local_path"],
        },
        "externalResult": {
            "status": "quarantined", "binderyDetached": True,
            "permanentDeletion": False, "replacementRequested": False,
            "resultId": 4, "fileId": 9001, "bookId": 101,
            "sha256": sha, "quarantinePath": str(retained),
        },
    }
    grab = {
        "planId": 7, "state": "succeeded", "attemptCount": 1,
        "boundary": {
            "candidateFingerprint": "fingerprint",
            "quarantineExecutionId": 9, "quarantineSha256": sha,
        },
        "externalResult": {
            "replacementAcquisitionId": 8, "queueId": 77,
            "admissionAttempted": False,
        },
    }
    queue = {"id": 77, "bookId": 101, "title": child["candidate_title"],
             "protocol": "usenet", "status": "importExternal"}
    monkeypatch.setattr(handoff, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(handoff, "result_by_id", lambda result_id: result)
    monkeypatch.setattr(handoff, "ebook_replacement_for_quarantine_plan",
                        lambda plan_id: child)
    monkeypatch.setattr(handoff, "quarantine_selection_by_result",
                        lambda result_id: selection)
    monkeypatch.setattr(handoff.core, "_existing",
                        lambda signature, action, index: {
                            "quarantine_exact_media": quarantine,
                            "reacquire_expected_media": grab,
                        }.get(action))
    monkeypatch.setattr(handoff, "_current_plan", lambda result_id: plan)
    monkeypatch.setattr(handoff, "associations_inside_path", lambda path: [])
    monkeypatch.setattr(handoff.workflow, "_result_and_book",
                        lambda result, client: ({}, "Book", "Author"))
    monkeypatch.setattr(handoff, "BinderyClient", lambda: object())
    monkeypatch.setattr(handoff.workflow, "_queue_payload",
                        lambda client: ([queue], False))
    monkeypatch.setattr(handoff, "list_staged_ebooks", lambda limit: {
        "items": [{"relativePath": fingerprint[0], "size": fingerprint[1],
                   "modifiedNs": fingerprint[2]}], "truncated": False,
    })
    monkeypatch.setattr(handoff, "resolve_staged_file",
                        lambda relative: (relative, Path(staged)))
    monkeypatch.setattr(handoff, "_verified_snapshot_matches",
                        lambda *args: True)
    return plan, child, retained, staged, queue


def test_exact_handoff_records_only_read_only_step(verified_handoff, monkeypatch):
    plan, child, _, _, _ = verified_handoff
    calls = []
    monkeypatch.setattr(handoff.core, "record_recovery_step_success",
                        lambda plan_id, step: calls.append((plan_id, step)) or {
                            **plan, "currentStep": 5,
                        })
    preview = handoff.quarantine_handoff_preview(7)
    assert preview["safeToRecord"] is True
    outcome = handoff.run_quarantine_verification_cycle(plan)
    assert outcome["state"] == "verified"
    assert outcome["externalMutationAttempted"] is False
    assert child["admission_id"] is None
    assert calls == [(7, 4)]


def test_changed_custody_queue_or_staging_refuses_handoff(verified_handoff, monkeypatch):
    plan, child, retained, staged, queue = verified_handoff
    retained.write_bytes(b"changed quarantine bytes")
    assert "RETAINED_QUARANTINE_BYTES" in {
        item["code"] for item in handoff.quarantine_handoff_preview(7)["checks"]
        if not item["ok"]
    }
    retained.write_bytes(b"exact quarantined bytes")
    queue["status"] = "downloading"
    assert handoff.quarantine_handoff_preview(7)["safeToRecord"] is False
    queue["status"] = "importExternal"
    staged.write_bytes(b"changed staged bytes")
    assert handoff.quarantine_handoff_preview(7)["safeToRecord"] is False
    child["admission_id"] = 11
    assert handoff.quarantine_handoff_preview(7)["safeToRecord"] is False
    monkeypatch.setattr(handoff.core, "record_recovery_step_success",
                        lambda *args: pytest.fail("unsafe proof advanced the plan"))
    monkeypatch.setattr(handoff.core, "block_recovery_plan",
                        lambda plan_id, message: {**plan, "state": "blocked"})
    outcome = handoff.run_quarantine_verification_cycle(plan)
    assert outcome["state"] == "blocked"
    assert outcome["externalMutationAttempted"] is False
