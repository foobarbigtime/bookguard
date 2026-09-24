from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager

import pytest

from app import publication_proof as proof


def _plan(step_index=3):
    return {
        "id": 81, "subjectId": 9, "signature": "exact-plan",
        "evidenceRevision": "exact-revision", "currentStep": step_index,
        "steps": [{"code": code, "externalMutation": index == 3}
                  for index, code in enumerate((*proof._READ_ONLY_STEPS,
                                                "prove_supported_no_replace_method",
                                                "retry_guarded_publication"))],
    }


def test_proof_only_touches_disposable_private_files(tmp_path, monkeypatch):
    executor = proof._PublicationPrimitiveProofExecutor()
    destination = tmp_path / "real-ebook.epub"
    staged = tmp_path / "staged.epub"
    staged.write_bytes(b"verified ebook")
    calls = []

    def fresh(plan, step):
        calls.append(step["code"])
        return {"ok": True, "planSignature": plan["signature"],
                "evidenceRevision": plan["evidenceRevision"],
                "checks": [{"code": "SAFE", "ok": True}],
                "destinationParent": str(tmp_path), "parentDevice": tmp_path.stat().st_dev,
                "parentInode": tmp_path.stat().st_ino, "stagedSha256": "a" * 64}

    monkeypatch.setattr(executor, "revalidate", fresh)
    boundary = fresh(_plan(), _plan()["steps"][3])
    result = executor.execute(_plan(), _plan()["steps"][3], boundary)

    assert result["publicationMethod"] in {"renameat2", "private-snapshot-link"}
    assert result["admissionPublished"] is False
    assert result["binderyScanRequested"] is False
    assert not destination.exists()
    assert staged.read_bytes() == b"verified ebook"
    assert list(tmp_path.iterdir()) == [staged]
    assert len(calls) == 2


def test_proof_rejects_unsafe_occupied_target(tmp_path, monkeypatch):
    executor = proof._PublicationPrimitiveProofExecutor()
    boundary = {"ok": True, "planSignature": "exact-plan",
                "evidenceRevision": "exact-revision",
                "checks": [{"code": "SAFE", "ok": True}],
                "destinationParent": str(tmp_path), "stagedSha256": "a" * 64}
    monkeypatch.setattr(executor, "revalidate", lambda plan, step: boundary)

    def unsafe_publish(source: Path, destination: Path):
        destination.write_bytes(source.read_bytes())
        return "unsafe"

    monkeypatch.setattr(proof, "_publish_no_replace", unsafe_publish)
    with pytest.raises(RuntimeError, match="overwrote"):
        executor.execute(_plan(), _plan()["steps"][3], boundary)
    assert not list(tmp_path.iterdir())


def test_proof_fails_closed_on_changed_boundary(tmp_path, monkeypatch):
    executor = proof._PublicationPrimitiveProofExecutor()
    boundary = {"ok": True, "planSignature": "exact-plan",
                "evidenceRevision": "exact-revision",
                "checks": [{"code": "SAFE", "ok": True}],
                "destinationParent": str(tmp_path), "stagedSha256": "a" * 64}
    monkeypatch.setattr(executor, "revalidate", lambda plan, step: {
        **boundary, "stagedSha256": "b" * 64
    })
    with pytest.raises(RuntimeError, match="changed"):
        executor.execute(_plan(), _plan()["steps"][3], boundary)
    assert not list(tmp_path.iterdir())


def test_completed_proof_never_proceeds_to_publication(monkeypatch):
    plan = _plan(4)
    monkeypatch.setattr(proof.core, "recovery_plan_by_id", lambda plan_id: plan)
    monkeypatch.setattr(proof.core, "attempt_automatic_step", lambda plan_id: pytest.fail(
        "publication must remain disabled"
    ))
    result = proof.run_publication_proof_cycle(plan)
    assert result["state"] == "paused"
    assert result["externalMutationAttempted"] is False


def test_preflight_rejects_changed_verified_identity(tmp_path, monkeypatch):
    plan = {**_plan(), "planKind": "RECOVER_ADMISSION_PUBLICATION",
            "subjectKind": "admission", "reasonCode":
            "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED", "resultId": 4,
            "bookId": 101, "path": "/data/media/books/Fixture.epub"}
    admission = {"id": 9, "result_id": 4, "book_id": 101,
                 "status": "failed", "failure_stage": "no_replace_unsupported",
                 "publication_method": None, "staged_sha256": "a" * 64,
                 "verification": {"safeToAdmit": True, "sha256": "a" * 64},
                 "stored_path": plan["path"], "local_path": "/books/Fixture.epub"}
    result = {"id": 4, "book_id": 101, "stored_path": plan["path"],
              "local_path": admission["local_path"]}

    @contextmanager
    def connection():
        yield object()

    monkeypatch.setattr(proof, "ebook_admission_by_id", lambda admission_id: admission)
    monkeypatch.setattr(proof, "result_by_id", lambda result_id: result)
    monkeypatch.setattr(proof, "local_conn", connection)
    monkeypatch.setattr(proof, "_admission_decisions", lambda conn, limit: [
        {"subjectKind": "admission", "subjectId": 9}
    ])
    monkeypatch.setattr(proof, "_build_plan", lambda conn, decision: plan)
    monkeypatch.setattr(proof, "admission_readiness", lambda client: {"ready": True})
    monkeypatch.setattr(proof, "_verified_staged_source", lambda row: tmp_path / "stage.epub")
    monkeypatch.setattr(proof, "_destination_for_result", lambda row, source: (
        tmp_path / "Fixture.epub", plan["path"]
    ))
    monkeypatch.setattr(proof, "_result_matches_book", lambda row, book: True)
    monkeypatch.setattr(proof, "_book_has_ebook", lambda book: False)
    monkeypatch.setattr(proof, "_exact_ebook_associations", lambda path: [])
    monkeypatch.setattr(proof, "BinderyClient", lambda: type("Client", (), {
        "get_book": lambda self, book_id: {"id": book_id}
    })())

    executor = proof._PublicationPrimitiveProofExecutor()
    boundary = executor.revalidate(plan, plan["steps"][3])
    assert boundary["ok"] is True
    admission["verification"]["sha256"] = "b" * 64
    changed = executor.revalidate(plan, plan["steps"][3])
    assert changed["ok"] is False
    assert {check["code"] for check in changed["checks"] if not check["ok"]} == {
        "VERIFIED_BYTES_UNCHANGED"
    }
