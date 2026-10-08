"""Check library sequencing, reservations, interruption and combined selection."""
import time

import pytest

from app import duplicates, library_check, scanner, unmatched, verifier
from tests import test_home_review

library = test_home_review.library


def wait_for(function):
    deadline = time.monotonic() + 5
    while not function():
        assert time.monotonic() < deadline, "background job did not finish"
        time.sleep(0.01)


@pytest.fixture
def workflow(monkeypatch):
    library_check._state.clear()
    library_check._state.update(status="idle", phase="idle", check_id=None, error=None)
    events = []
    scan = {"id": "scan-1", "status": "complete"}
    job = {"status": "idle"}
    unassigned = {"running": False, "error": "", "checked": 0}
    monkeypatch.setattr(scanner, "scan_is_running", lambda: False)
    monkeypatch.setattr(library_check, "latest_scan", lambda: scan)
    monkeypatch.setattr(verifier, "verification_job_status", lambda: dict(job))
    monkeypatch.setattr(unmatched, "check_status", lambda: dict(unassigned))
    monkeypatch.setattr(duplicates, "fix_status", lambda: {"running": False})

    def scan_start(**kwargs):
        events.append(("scan", kwargs))
        return "scan-1"

    def verify_start(classification, **kwargs):
        events.append((classification, kwargs))
        job.update(status="complete", job_id="proof-job", processed=3,
                   postponed=1, postponedReason="Catalogue unavailable", cacheHits=2)
        return "proof-job"

    def unmatched_start(**kwargs):
        events.append(("unassigned", kwargs))
        unassigned["checked"] = 4
        return True

    monkeypatch.setattr(scanner, "start_scan", scan_start)
    monkeypatch.setattr(verifier, "start_verification_job", verify_start)
    monkeypatch.setattr(unmatched, "start_check", unmatched_start)
    yield events, scan, job, unassigned
    wait_for(lambda: library_check._state["status"] != "running")
    library_check._state.clear()
    library_check._state.update(status="idle", phase="idle", check_id=None, error=None)


def test_check_sequences_workers_and_preserves_postponements(workflow):
    events, _, _, _ = workflow
    check_id = library_check.start_library_check()
    wait_for(lambda: library_check._state["status"] != "running")
    assert [event[0] for event in events] == ["scan", "ALL", "unassigned"]
    assert all(event[1]["check_id"] == check_id for event in events)
    assert events[1][1]["expected_scan_id"] == "scan-1"
    assert library_check._state["status"] == "complete"
    assert library_check._state["verification_result"]["postponed"] == 1
    assert library_check._state["unmatched_result"]["checked"] == 4


@pytest.mark.parametrize("status", ["failed", "cancelled", "stopped"])
def test_unfinished_scan_never_starts_verification(workflow, status):
    events, scan, _, _ = workflow
    scan["status"] = status
    assert library_check.start_library_check()
    wait_for(lambda: library_check._state["status"] != "running")
    assert [event[0] for event in events] == ["scan"]
    assert library_check._state["status"] == "failed"
    assert library_check._state["phase"] == "scan"


def test_different_latest_scan_is_not_verified(workflow):
    events, scan, _, _ = workflow
    scan["id"] = "another-scan"
    assert library_check.start_library_check()
    wait_for(lambda: library_check._state["status"] != "running")
    assert [event[0] for event in events] == ["scan"]


def test_failed_verification_does_not_start_unassigned_check(workflow, monkeypatch):
    events, _, job, _ = workflow

    def fail(*args, **kwargs):
        job.update(status="failed", job_id="proof-job", error="Probe failed")
        return "proof-job"

    monkeypatch.setattr(verifier, "start_verification_job", fail)
    assert library_check.start_library_check()
    wait_for(lambda: library_check._state["status"] != "running")
    assert [event[0] for event in events] == ["scan"]
    assert library_check._state["phase"] == "verification"
    assert library_check._state["error"] == "Probe failed"


def test_unassigned_outage_retains_verification(workflow):
    _, _, _, unassigned = workflow
    unassigned["error"] = "Bindery unavailable"
    assert library_check.start_library_check()
    wait_for(lambda: library_check._state["status"] != "running")
    assert library_check._state["phase"] == "unmatched"
    assert "Bindery unavailable" in library_check._state["error"]


def test_existing_job_refuses_combined_check(workflow):
    events, _, job, _ = workflow
    job["status"] = "running"
    assert library_check.start_library_check() is None
    assert events == []


def test_reservation_blocks_independent_job_starts(monkeypatch):
    library_check._state.update(status="running", check_id="reserved")
    try:
        assert scanner.start_scan() is None
        assert verifier.start_verification_job() is None
        assert unmatched.start_check() is False
        assert duplicates.start_fix() is False
        assert library_check.start_library_check() is None
    finally:
        library_check._state.update(status="idle", check_id=None)


def test_all_verifies_review_and_reject_but_not_resolved_or_pass(library, monkeypatch):
    seen = []
    monkeypatch.setattr(scanner, "scan_is_running", lambda: False)
    monkeypatch.setattr(verifier, "verify_result", lambda row: (seen.append(row) or {"verdict": "VERIFIED_CORRECT", "cached": True}))
    with verifier._job_lock:
        verifier._job_state["status"] = "idle"
    assert verifier.start_verification_job("ALL", expected_scan_id="home-scan")
    wait_for(lambda: verifier.verification_job_status()["status"] != "running")
    assert {row["classification"] for row in seen} == {"REVIEW", "REJECT"}
    assert len(seen) == 9
    assert "Reviewed already" not in {row["title"] for row in seen}
    assert verifier.verification_job_status()["cacheHits"] == 9


def test_all_refuses_a_superseded_scan(library):
    with pytest.raises(RuntimeError, match="latest scan changed"):
        verifier.start_verification_job("ALL", expected_scan_id="old-scan")


def test_all_does_not_truncate_at_the_legacy_verification_limit(monkeypatch):
    rows = [{"id": i, "author": "A", "title": str(i)} for i in range(10001)]
    seen = []
    monkeypatch.setattr(scanner, "scan_is_running", lambda: False)
    monkeypatch.setattr(verifier, "latest_scan", lambda: {"id": "large", "status": "complete"})
    monkeypatch.setattr(verifier, "latest_review_results", lambda: rows)
    monkeypatch.setattr(verifier, "triage_states", lambda rows: {row["id"]: {"resolved": False} for row in rows})
    monkeypatch.setattr(verifier, "verify_result", lambda row: (seen.append(row["id"]) or {"verdict": "VERIFIED_CORRECT"}))
    with verifier._job_lock:
        verifier._job_state["status"] = "idle"
    assert verifier.start_verification_job("ALL")
    wait_for(lambda: verifier.verification_job_status()["status"] != "running")
    assert len(seen) == 10001
