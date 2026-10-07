"""Verification avoids copying unchanged books and records where time goes.

An unchanged book with a saved verdict is answered from a read-only hash,
without the private snapshot copy and fsync. Anything else (a changed file, a
forced check, a file the snapshot rules refuse) still takes the full path.
"""

from __future__ import annotations

from dataclasses import dataclass
import time

import pytest

from app import verifier
from app.config import settings


@dataclass
class _Extracted:
    metadata: dict
    text: str
    identifiers: list
    notes: list
    front_text: str
    source: str


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(settings, "config_dir", str(config))
    monkeypatch.setattr(settings, "verification_enabled", True)
    monkeypatch.setattr(settings, "verification_snapshot_max_bytes", 1024 * 1024)
    verifier.init_verification_db()
    monkeypatch.setattr(verifier, "inspect_ebook_security", lambda path, **kw: {
        "safe": True, "checks": {}, "failures": [], "message": "ok"})
    monkeypatch.setattr(verifier, "extract_ebook_identity", lambda path: _Extracted(
        {"title": "Bel Canto", "author": "Ann Patchett"},
        "Bel Canto by Ann Patchett " * 40, [], [], "Bel Canto\nAnn Patchett", "test"))
    monkeypatch.setattr(verifier, "bindery_series_names", lambda _id: [])
    monkeypatch.setattr(verifier, "isbn_evidence", lambda _id, _ids: {})
    source = tmp_path / "Bel Canto - Ann Patchett.txt"
    source.write_text("Bel Canto by Ann Patchett", encoding="utf-8")
    result = {
        "id": 1, "scan_id": "scan-1", "file_id": 2, "book_id": 3, "format": "ebook",
        "author": "Ann Patchett", "title": "Bel Canto", "local_path": str(source),
    }
    return result, source


@pytest.fixture
def snapshot_calls(monkeypatch):
    calls: list[str] = []
    real = verifier.verification_snapshot

    def counting(path, **kwargs):
        calls.append(str(path))
        return real(path, **kwargs)

    monkeypatch.setattr(verifier, "verification_snapshot", counting)
    return calls


def test_unchanged_book_is_answered_from_cache_without_a_snapshot(env, snapshot_calls):
    result, _source = env

    first = verifier.verify_result(result)
    second = verifier.verify_result(result)

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["verdict"] == first["verdict"]
    assert len(snapshot_calls) == 1


def test_changed_book_is_checked_again(env, snapshot_calls):
    result, source = env

    verifier.verify_result(result)
    source.write_text("Bel Canto by Ann Patchett, revised", encoding="utf-8")
    again = verifier.verify_result(result)

    assert again["cached"] is False
    assert len(snapshot_calls) == 2


def test_cached_proof_is_visible_in_the_new_scan_without_changing_history(env, snapshot_calls, monkeypatch):
    from app.library_review import latest_verifications

    result, _source = env
    old = verifier.verify_result(result)
    current_result = {**result, "id": 2, "scan_id": "scan-2"}

    current = verifier.verify_result(current_result)
    repeated = verifier.verify_result(current_result)

    assert current["cached"] is True
    assert current["id"] != old["id"]
    assert (current["result_id"], current["scan_id"]) == (2, "scan-2")
    assert repeated["id"] == current["id"]
    assert len(snapshot_calls) == 1
    with verifier.local_conn() as conn:
        historical = conn.execute("SELECT * FROM content_verifications WHERE id=?", (old["id"],)).fetchone()
        assert (historical["result_id"], historical["scan_id"]) == (1, "scan-1")
        assert conn.execute("SELECT COUNT(*) FROM content_verifications").fetchone()[0] == 2
    assert latest_verifications([2])[2]["verdict"] == current["verdict"]
    monkeypatch.setattr(verifier, "latest_scan", lambda: {"id": "scan-2"})
    assert verifier.verification_summary() == {current["verdict"]: 1}


def test_cached_audio_proof_gets_a_current_scan_receipt(env, monkeypatch):
    result, source = env
    result = {**result, "format": "audiobook"}
    calls = []
    monkeypatch.setattr(verifier, "media_set_fingerprint", lambda path: "audio:unchanged")

    def evidence(row, target):
        calls.append(target)
        return {"verdict": "VERIFIED_CORRECT", "confidence": 95,
                "source": "audiobook-evidence", "evidence": {"explanation": "Tags agree."}}

    monkeypatch.setattr(verifier, "build_audiobook_evidence", evidence)
    old = verifier.verify_result(result)
    current = verifier.verify_result({**result, "id": 2, "scan_id": "scan-2"})

    assert current["cached"] is True
    assert (current["result_id"], current["scan_id"]) == (2, "scan-2")
    assert current["id"] != old["id"]
    assert calls == [str(source)]


def test_newer_inconclusive_check_does_not_fall_back_to_an_older_cached_proof(env, monkeypatch):
    result, _source = env
    result = {**result, "format": "audiobook"}
    calls = []
    monkeypatch.setattr(verifier, "media_set_fingerprint", lambda path: "audio:unchanged")

    def evidence(row, target):
        calls.append(row["id"])
        return {"verdict": "VERIFIED_CORRECT" if len(calls) == 1 else "INSUFFICIENT_EVIDENCE",
                "confidence": 95 if len(calls) == 1 else 0, "source": "audiobook-evidence",
                "evidence": {} if len(calls) == 1 else {"malwareScanInconclusive": True}}

    monkeypatch.setattr(verifier, "build_audiobook_evidence", evidence)
    verifier.verify_result(result)
    verifier.verify_result({**result, "id": 2, "scan_id": "scan-2"}, force=True)

    current = verifier.verify_result({**result, "id": 3, "scan_id": "scan-3"})

    assert current["cached"] is False
    assert current["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert calls == [1, 2, 3]


@pytest.mark.parametrize("attribute, value", [
    ("title_min_shared_words", 99),
    ("allow_author_surname_match", False),
    ("author_aliases", ["Ann Patchett=A. Patchett"]),
])
def test_matching_policy_change_does_not_reuse_an_old_verdict(env, snapshot_calls, monkeypatch, attribute, value):
    result, _source = env
    # Make both sides of the policy transition explicit, independent of host defaults.
    monkeypatch.setattr(settings, attribute, {"title_min_shared_words": 2,
                        "allow_author_surname_match": True, "author_aliases": []}[attribute])
    verifier.verify_result(result)
    monkeypatch.setattr(settings, attribute, value)

    again = verifier.verify_result(result)

    assert again["cached"] is False
    assert len(snapshot_calls) == 2


def test_series_context_change_does_not_reuse_an_old_verdict(env, snapshot_calls, monkeypatch):
    result, _source = env
    verifier.verify_result(result)
    monkeypatch.setattr(verifier, "bindery_series_names", lambda _id: ["A recorded series"])

    again = verifier.verify_result(result)

    assert again["cached"] is False
    assert len(snapshot_calls) == 2


def test_forced_check_always_takes_the_full_path(env, snapshot_calls):
    result, _source = env

    verifier.verify_result(result)
    forced = verifier.verify_result(result, force=True)

    assert forced["cached"] is False
    assert len(snapshot_calls) == 2


def test_a_file_the_snapshot_refuses_is_not_served_from_the_hash_check(env, monkeypatch):
    result, _source = env
    monkeypatch.setattr(settings, "verification_snapshot_max_bytes", 4)

    first = verifier.verify_result(result)
    second = verifier.verify_result(result)

    assert first["source"] == second["source"] == "source-snapshot"
    assert second["cached"] is False


def test_fresh_verification_records_stage_timings(env):
    result, _source = env

    timings = verifier.verify_result(result)["evidence"]["timings"]

    assert set(timings) == {"snapshotMs", "safetyChecksMs", "identityMs"}
    assert all(isinstance(value, int) and value >= 0 for value in timings.values())


def test_verification_job_reports_cache_hits_and_elapsed_time(monkeypatch):
    rows = [{"author": "A", "title": str(i)} for i in range(3)]
    monkeypatch.setattr(verifier, "latest_scan", lambda: {"id": "s", "status": "complete"})
    monkeypatch.setattr(verifier, "latest_results", lambda **kw: rows)
    monkeypatch.setattr(verifier, "triage_state", lambda row: {})
    outcomes = iter([{"verdict": "VERIFIED_CORRECT", "cached": True},
                     {"verdict": "VERIFIED_CORRECT", "cached": False},
                     {"verdict": "INSUFFICIENT_EVIDENCE", "cached": True}])
    monkeypatch.setattr(verifier, "verify_result", lambda row: next(outcomes))
    with verifier._job_lock:
        verifier._job_state["status"] = "idle"

    assert verifier.start_verification_job("REVIEW")
    for _ in range(200):
        if verifier.verification_job_status()["status"] == "complete":
            break
        time.sleep(0.01)
    status = verifier.verification_job_status()

    assert status["status"] == "complete"
    assert status["processed"] == 3
    assert status["cacheHits"] == 2
    assert isinstance(status["elapsedSeconds"], int)
