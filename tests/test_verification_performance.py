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
    ocr: dict | None = None


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
    monkeypatch.setattr(verifier, "bindery_series_context", lambda _id: [])
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


def _ocr_pdf(env, monkeypatch, *, error="", error_code=""):
    result, source = env
    pdf = source.with_suffix(".pdf")
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result = {**result, "local_path": str(pdf)}
    monkeypatch.setattr(settings, "verification_ocr", True)
    monkeypatch.setattr(settings, "verification_ocr_pages", 3)
    monkeypatch.setattr(settings, "verification_ocr_languages", "eng")

    def extract(path):
        attempted = settings.verification_ocr
        text = "Bel Canto\nAnn Patchett" if attempted and not error else ""
        return _Extracted(
            {"title": "Bel Canto", "author": "Ann Patchett"}, text, [], [], text,
            "native-pdf+ocr" if text else "native-pdf",
            {"attempted": True, "pages": 1, "chars": len(text), "error": error,
             "errorCode": error_code, "policy": verifier.ocr_policy()} if attempted else None,
        )

    monkeypatch.setattr(verifier, "extract_ebook_identity", extract)
    return result


def test_unchanged_successful_ocr_reuses_its_recorded_policy(env, snapshot_calls, monkeypatch):
    result = _ocr_pdf(env, monkeypatch)
    first = verifier.verify_result(result)
    again = verifier.verify_result(result)
    assert first["evidence"]["ocrPolicy"] == verifier.ocr_policy()
    assert again["cached"] is True
    assert len(snapshot_calls) == 1


def test_ocr_policy_is_the_one_used_when_settings_change_during_extraction(env, snapshot_calls, monkeypatch):
    result = _ocr_pdf(env, monkeypatch)
    original = verifier.extract_ebook_identity

    def change_settings(path):
        extracted = original(path)
        monkeypatch.setattr(settings, "verification_ocr_pages", 6)
        return extracted

    monkeypatch.setattr(verifier, "extract_ebook_identity", change_settings)
    first = verifier.verify_result(result)
    assert first["evidence"]["ocrPolicy"]["pages"] == 3
    assert verifier.verify_result(result)["cached"] is False
    assert len(snapshot_calls) == 2


@pytest.mark.parametrize("attribute, value", [
    ("verification_ocr_pages", 6),
    ("verification_ocr_languages", "fra"),
    ("verification_ocr", False),
])
def test_changed_ocr_settings_recheck_an_affected_pdf(env, snapshot_calls, monkeypatch, attribute, value):
    result = _ocr_pdf(env, monkeypatch)
    verifier.verify_result(result)
    monkeypatch.setattr(settings, attribute, value)
    again = verifier.verify_result(result)
    assert again["cached"] is False
    assert len(snapshot_calls) == 2
    assert verifier.verify_result(result)["cached"] is True
    if attribute == "verification_ocr":
        assert again["verdict"] == "INSUFFICIENT_EVIDENCE"


def test_failed_ocr_is_retried_when_the_dependency_recovers(env, snapshot_calls, monkeypatch):
    result = _ocr_pdf(env, monkeypatch, error="Tesseract unavailable")
    failed = verifier.verify_result(result)
    assert failed["verdict"] == "INSUFFICIENT_EVIDENCE"
    _ocr_pdf(env, monkeypatch)
    recovered = verifier.verify_result(result)
    assert recovered["cached"] is False
    assert recovered["verdict"] == "VERIFIED_CORRECT"
    assert len(snapshot_calls) == 2


@pytest.mark.parametrize("error_code", ["preflight_error", "render_error"])
def test_unchanged_unreadable_pdf_reuses_failed_ocr_result(env, snapshot_calls, monkeypatch, error_code):
    result = _ocr_pdf(env, monkeypatch, error="Damaged or protected PDF.", error_code=error_code)
    first = verifier.verify_result(result)
    second = verifier.verify_result(result)
    assert first["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert second["cached"] is True
    assert len(snapshot_calls) == 1
    monkeypatch.setattr(settings, "verification_ocr_pages", 6)
    assert verifier.verify_result(result)["cached"] is False
    assert len(snapshot_calls) == 2


@pytest.mark.parametrize("error_code", ["dependency_unavailable", "render_timeout", "render_process_error", "render_output_limit", "ocr_error"])
def test_transient_ocr_errors_remain_retryable(env, snapshot_calls, monkeypatch, error_code):
    result = _ocr_pdf(env, monkeypatch, error="Temporary OCR failure.", error_code=error_code)
    verifier.verify_result(result)
    assert verifier.verify_result(result)["cached"] is False
    assert len(snapshot_calls) == 2


def test_successful_ocr_with_no_text_is_cached_until_the_page_policy_changes(env, snapshot_calls, monkeypatch):
    result = _ocr_pdf(env, monkeypatch, error="OCR found no text.", error_code="no_text")
    verifier.verify_result(result)
    assert verifier.verify_result(result)["cached"] is True
    assert len(snapshot_calls) == 1
    monkeypatch.setattr(settings, "verification_ocr_pages", 6)
    assert verifier.verify_result(result)["cached"] is False
    assert len(snapshot_calls) == 2


def test_ocr_setting_changes_keep_unrelated_native_proof_cached(env, snapshot_calls, monkeypatch):
    result, _source = env
    verifier.verify_result(result)
    monkeypatch.setattr(settings, "verification_ocr_pages", 6)
    monkeypatch.setattr(settings, "verification_ocr_languages", "fra")
    assert verifier.verify_result(result)["cached"] is True
    assert len(snapshot_calls) == 1


def test_an_old_author_only_correct_verdict_is_rechecked(env, snapshot_calls):
    result, source = env
    old = verifier._save_verification(
        result, str(source), verifier._file_fingerprint(str(source)),
        "VERIFIED_CORRECT", 90, "native-text",
        {"content": {"expected_signal": {"title_found": False, "author_found": True},
                     "embedded_signal": {"title_found": False, "author_found": True}}},
    )
    fresh = verifier.verify_result(result)
    assert fresh["cached"] is False and fresh["confidence"] == 99
    assert fresh["id"] == old["id"]  # Same-result upsert; other scans' historical receipts stay intact.
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
    monkeypatch.setattr(verifier, "bindery_series_context", lambda _id: ["A recorded series"])

    again = verifier.verify_result(result)

    assert again["cached"] is False
    assert len(snapshot_calls) == 2


def test_rescan_receipts_keep_original_proof_without_flooding_activity(env, snapshot_calls):
    from app.history import operation_history
    from app.library_review import latest_verifications

    result, _source = env
    original = verifier.verify_result(result)
    for n in range(2, 6):
        receipt = verifier.verify_result({**result, "id": n, "scan_id": f"scan-{n}"})
        assert receipt["evidence"]["cacheReuse"] == {
            "verificationId": original["id"], "scanId": original["scan_id"], "verifiedAt": original["updated_at"],
        }
        assert latest_verifications([n])[n]["verdict"] == original["verdict"]
    events = [e for e in operation_history(250)["items"] if e["kind"] == "verification"]
    assert len(snapshot_calls) == 1
    assert len(events) == 1
    with verifier.local_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM content_verifications").fetchone()[0] == 5
        # Filtering must precede the feed's source limit, even with many receipts.
        from app.history import _verification_events
        assert len(_verification_events(conn, 1)) == 1


def test_forced_verification_is_still_a_real_activity_event(env, snapshot_calls):
    from app.history import operation_history

    result, _source = env
    verifier.verify_result(result)
    verifier.verify_result({**result, "id": 2, "scan_id": "scan-2"})
    verifier.verify_result({**result, "id": 2, "scan_id": "scan-2"}, force=True)

    events = [e for e in operation_history(250)["items"] if e["kind"] == "verification"]
    assert len(snapshot_calls) == 2
    assert len(events) == 2


def test_legacy_cache_chain_resolves_to_original_proof(env):
    import json

    result, _source = env
    original = verifier.verify_result(result)
    second = verifier.verify_result({**result, "id": 2, "scan_id": "scan-2"})
    third = verifier.verify_result({**result, "id": 3, "scan_id": "scan-3"})
    # Reproduce the v27 chain format, without its original timestamp.
    with verifier.local_conn() as conn:
        for receipt, parent in [(second, original), (third, second)]:
            evidence = {**receipt["evidence"], "cacheReuse": {
                "verificationId": parent["id"], "scanId": parent["scan_id"],
            }}
            conn.execute("UPDATE content_verifications SET evidence_json=? WHERE id=?",
                         (json.dumps(evidence), receipt["id"]))
        conn.commit()

    current = verifier.verify_result({**result, "id": 4, "scan_id": "scan-4"})

    assert current["evidence"]["cacheReuse"] == {
        "verificationId": original["id"], "scanId": "scan-1", "verifiedAt": original["updated_at"],
    }


def test_series_context_is_loaded_once_and_used_for_both_cache_and_evidence(env, monkeypatch):
    result, _source = env
    calls = []
    classified = []
    real_classify = verifier.classify_identity

    def context(book_id):
        calls.append(book_id)
        return ["Recorded series"]

    def classify(row, *args):
        classified.append(row["series"])
        return real_classify(row, *args)

    monkeypatch.setattr(verifier, "bindery_series_context", context)
    monkeypatch.setattr(verifier, "classify_identity", classify)

    verifier.verify_result(result)

    assert calls == [result["book_id"]]
    assert classified == [["Recorded series"]]


def test_unavailable_catalogue_postpones_verification_without_rekeying_history(env, snapshot_calls, monkeypatch):
    result, _source = env
    original = verifier.verify_result(result)
    monkeypatch.setattr(verifier, "bindery_series_context", lambda _id: None)

    with pytest.raises(verifier.CatalogueUnavailable, match="verification is postponed"):
        verifier.verify_result({**result, "id": 2, "scan_id": "scan-2"})

    assert verifier.verification_for_result(result) is None
    assert len(snapshot_calls) == 1
    with verifier.local_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM content_verifications").fetchone()[0] == 1
    monkeypatch.setattr(verifier, "bindery_series_context", lambda _id: [])
    current = verifier.verify_result({**result, "id": 2, "scan_id": "scan-2"})
    assert current["cached"] is True
    assert current["evidence"]["cacheReuse"]["verificationId"] == original["id"]


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


def test_catalogue_outage_postpones_one_book_and_the_job_continues(monkeypatch):
    rows = [{"author": "A", "title": str(i)} for i in range(3)]
    monkeypatch.setattr(verifier, "latest_scan", lambda: {"id": "s", "status": "complete"})
    monkeypatch.setattr(verifier, "latest_results", lambda **kw: rows)
    monkeypatch.setattr(verifier, "triage_state", lambda row: {})
    seen = []

    def verify(row):
        seen.append(row["title"])
        if row["title"] == "0":
            raise verifier.CatalogueUnavailable("Bindery series data is unavailable.")
        return {"verdict": "VERIFIED_CORRECT", "cached": False}

    monkeypatch.setattr(verifier, "verify_result", verify)
    with verifier._job_lock:
        verifier._job_state["status"] = "idle"

    assert verifier.start_verification_job("REVIEW")
    for _ in range(200):
        if verifier.verification_job_status()["status"] != "running":
            break
        time.sleep(0.01)
    status = verifier.verification_job_status()

    assert status["status"] == "complete"
    assert seen == ["0", "1", "2"]
    assert status["processed"] == 3
    assert status["postponed"] == 1
    assert "unavailable" in status["postponedReason"]
    assert status["counts"] == {"VERIFIED_CORRECT": 2}
    assert status["error"] is None


def test_other_errors_still_fail_the_job(monkeypatch):
    rows = [{"author": "A", "title": "0"}, {"author": "A", "title": "1"}]
    monkeypatch.setattr(verifier, "latest_scan", lambda: {"id": "s", "status": "complete"})
    monkeypatch.setattr(verifier, "latest_results", lambda **kw: rows)
    monkeypatch.setattr(verifier, "triage_state", lambda row: {})
    monkeypatch.setattr(verifier, "verify_result", lambda row: (_ for _ in ()).throw(RuntimeError("boom")))
    with verifier._job_lock:
        verifier._job_state["status"] = "idle"

    assert verifier.start_verification_job("REVIEW")
    for _ in range(200):
        if verifier.verification_job_status()["status"] != "running":
            break
        time.sleep(0.01)

    assert verifier.verification_job_status()["status"] == "failed"
