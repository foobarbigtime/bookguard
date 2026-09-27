from pathlib import Path

from app import quarantine_replacement as preview
from app.file_safety import sha256_file


def _fixture(monkeypatch, tmp_path):
    root = tmp_path / "quarantine"
    retained = root / "101" / "Unsafe.epub"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"disposable unsafe bytes")
    sha = sha256_file(retained)
    plan = {
        "id": 7, "signature": "current", "evidenceRevision": "revision",
        "planKind": "QUARANTINE_UNSAFE_MEDIA", "subjectKind": "result",
        "subjectId": "4", "resultId": 4, "bookId": 101,
        "path": "/data/media/books/Unsafe.epub",
        "state": "ready", "currentStep": 3,
        "steps": [{"code": "revalidate_unsafe_verdict"},
                  {"code": "capture_exact_source_identity"},
                  {"code": "quarantine_exact_media"},
                  {"code": "reacquire_expected_media"}],
    }
    result = {
        "id": 4, "file_id": 9001, "book_id": 101,
        "local_path": str(tmp_path / "books" / "Unsafe.epub"),
        "stored_path": "/data/media/books/Unsafe.epub",
    }
    receipt = {
        "state": "succeeded", "planId": 7, "attemptCount": 1,
        "boundary": {
            "expectedSha256": sha, "storedPath": result["stored_path"],
            "localPath": result["local_path"], "fileId": 9001, "bookId": 101,
        },
        "externalResult": {
            "status": "quarantined", "resultId": 4, "fileId": 9001,
            "bookId": 101, "sha256": sha, "quarantinePath": str(retained),
            "binderyDetached": True, "permanentDeletion": False,
            "replacementRequested": False,
        },
    }
    monkeypatch.setattr(preview.settings, "quarantine_root", str(root))
    monkeypatch.setattr(preview, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(preview, "result_by_id", lambda result_id: result)
    monkeypatch.setattr(preview, "_current_plan", lambda result_id: plan)
    monkeypatch.setattr(preview, "_prior_acquisition", lambda result_id: False)
    monkeypatch.setattr(preview, "associations_inside_path", lambda path: [])
    monkeypatch.setattr(preview, "_result_and_book", lambda result, client: ({}, "", ""))
    monkeypatch.setattr(preview.core, "_existing", lambda *args: receipt)
    return plan, result, receipt, retained


def test_quarantine_custody_preview_requires_exact_retained_bytes(monkeypatch, tmp_path):
    _, _, _, retained = _fixture(monkeypatch, tmp_path)

    good = preview.quarantine_replacement_preview(7, client=object())
    assert good["safeForCandidateReview"] is True
    assert good["liveGrabEnabled"] is False

    retained.write_bytes(b"tampered disposable bytes")
    changed = preview.quarantine_replacement_preview(7, client=object())
    assert changed["safeForCandidateReview"] is False
    assert next(check for check in changed["checks"]
                if check["code"] == "QUARANTINE_BYTES_MATCH_RECEIPT")["ok"] is False


def test_quarantine_custody_refuses_restored_source_or_foreign_owner(monkeypatch, tmp_path):
    _, result, _, _ = _fixture(monkeypatch, tmp_path)
    source = Path(result["local_path"])
    source.parent.mkdir(parents=True)
    source.write_bytes(b"restored")
    monkeypatch.setattr(preview, "associations_inside_path",
                        lambda path: [{"file_id": 9001}])

    inspected = preview.quarantine_replacement_preview(7, client=object())

    assert inspected["safeForCandidateReview"] is False
    assert {check["code"] for check in inspected["checks"] if not check["ok"]} == {
        "ORIGINAL_SOURCE_ABSENT", "EXACT_BINDERY_ASSOCIATION_ABSENT",
    }


def test_quarantine_custody_refuses_wrong_receipt_or_symlink(monkeypatch, tmp_path):
    _, _, receipt, retained = _fixture(monkeypatch, tmp_path)
    receipt["attemptCount"] = 2
    assert preview.quarantine_replacement_preview(7, client=object())[
        "safeForCandidateReview"] is False

    receipt["attemptCount"] = 1
    retained.rename(retained.with_suffix(".saved"))
    retained.symlink_to(retained.with_suffix(".saved"))
    inspected = preview.quarantine_replacement_preview(7, client=object())
    assert inspected["safeForCandidateReview"] is False


def test_quarantine_custody_refuses_changed_plan_identity(monkeypatch, tmp_path):
    plan, _, _, _ = _fixture(monkeypatch, tmp_path)
    plan["bookId"] = 202

    inspected = preview.quarantine_replacement_preview(7, client=object())

    assert inspected["safeForCandidateReview"] is False
    assert next(check for check in inspected["checks"]
                if check["code"] == "CURRENT_DECISION_IDENTITY")["ok"] is False
