from __future__ import annotations

from contextlib import contextmanager
import sqlite3

import pytest

from app import publication_registration as registration


def _plan() -> dict:
    steps = [
        "revalidate_failed_admission", "revalidate_staged_bytes",
        "revalidate_admission_topology", "prove_supported_no_replace_method",
        "retry_guarded_publication", "scan_and_reconcile_registration",
    ]
    return {
        "id": 8, "subjectId": 9, "subjectKind": "admission",
        "resultId": 4, "bookId": 101, "path": "/data/media/books/Fixture.epub",
        "signature": "exact", "evidenceRevision": "revision", "currentStep": 5,
        "planKind": "RECOVER_ADMISSION_PUBLICATION",
        "reasonCode": "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED",
        "steps": [{"code": code, "externalMutation": index >= 3}
                  for index, code in enumerate(steps)],
    }


def _boundary(state="scan_required", status="published") -> dict:
    return {
        "ok": True, "planSignature": "exact", "evidenceRevision": "revision",
        "checks": [{"code": "CURRENT", "ok": True}],
        "status": status, "registrationState": state,
        "stagedSha256": "a" * 64, "publicationMethod": "renameat2",
        "storedPath": "/data/media/books/Fixture.epub",
    }


def test_scan_requires_completed_exact_publication(monkeypatch):
    monkeypatch.setattr(registration.core, "_existing", lambda *args: None)
    with pytest.raises(registration.core.AutomaticExecutionBlocked,
                       match="no completed publication"):
        registration._published_receipt(_plan())


def test_exact_scan_request_is_single_and_stops_after_durable_transition(monkeypatch):
    calls = []
    executor = registration._KnownPublicationRegistrationExecutor()
    monkeypatch.setattr(executor, "revalidate", lambda plan, step: _boundary())

    class FakeClient:
        def scan_library(self):
            calls.append("scan")

    monkeypatch.setattr(registration, "BinderyClient", FakeClient)
    monkeypatch.setattr(registration, "_scan_requested",
                        lambda plan, method, sha: calls.append("durable"))
    result = executor.execute(_plan(), _plan()["steps"][5], _boundary())
    assert calls == ["scan", "durable"]
    assert result["status"] == "scan_requested"
    assert result["scanRequested"] is True
    assert result["libraryBytesChanged"] is False


def test_independently_registered_owner_is_adopted_without_scan(monkeypatch):
    calls = []
    executor = registration._KnownPublicationRegistrationExecutor()
    monkeypatch.setattr(executor, "revalidate",
                        lambda plan, step: _boundary("registered"))

    class FakeClient:
        def scan_library(self):
            pytest.fail("already registered owner must not trigger a scan")

    monkeypatch.setattr(registration, "BinderyClient", FakeClient)
    monkeypatch.setattr(registration, "reconcile_admission",
                        lambda admission_id, client, allow_scan: calls.append(allow_scan)
                        or {"status": "registered"})
    result = executor.execute(
        _plan(), _plan()["steps"][5], _boundary("registered")
    )
    assert calls == [False]
    assert result["externalMutationPerformed"] is False


def test_unproven_scan_conflict_does_not_mark_scan_requested(monkeypatch):
    executor = registration._KnownPublicationRegistrationExecutor()
    monkeypatch.setattr(executor, "revalidate", lambda plan, step: _boundary())

    class FakeClient:
        def scan_library(self):
            raise registration.BinderyClientError("HTTP 409: request refused")

    monkeypatch.setattr(registration, "BinderyClient", FakeClient)
    monkeypatch.setattr(registration, "_scan_requested", lambda *args:
                        pytest.fail("unproven conflict must not advance admission"))
    with pytest.raises(registration.BinderyClientError, match="request refused"):
        executor.execute(_plan(), _plan()["steps"][5], _boundary())


def test_running_unproven_scan_never_retries(monkeypatch):
    executor = registration._KnownPublicationRegistrationExecutor()
    monkeypatch.setattr(executor, "_observe", lambda plan, states: _boundary())
    monkeypatch.setattr(registration, "BinderyClient", lambda: pytest.fail(
        "a running unproven scan cannot call Bindery again"
    ))
    with pytest.raises(registration.core.AutomaticExecutionBlocked,
                       match="second scan is refused"):
        executor.reconcile_uncertain(
            _plan(), _plan()["steps"][5], {"boundary": _boundary()}
        )


def test_scan_state_transition_requires_exact_published_identity(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ebook_admissions (
            id INTEGER, status TEXT, publication_method TEXT, staged_sha256 TEXT,
            result_id INTEGER, book_id INTEGER, stored_path TEXT, error TEXT,
            updated_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO ebook_admissions VALUES (9, 'published', 'renameat2', ?, "
        "4, 101, ?, NULL, '')",
        ("a" * 64, _plan()["path"]),
    )
    conn.commit()

    @contextmanager
    def local_conn():
        yield conn

    monkeypatch.setattr(registration, "local_conn", local_conn)
    monkeypatch.setattr(registration, "utc_now", lambda: "now")
    with pytest.raises(registration.AdmissionSafetyError, match="changed"):
        registration._scan_requested(_plan(), "renameat2", "b" * 64)
    registration._scan_requested(_plan(), "renameat2", "a" * 64)
    assert conn.execute("SELECT status FROM ebook_admissions").fetchone()[0] == "scan_requested"
    with pytest.raises(registration.AdmissionSafetyError, match="changed"):
        registration._scan_requested(_plan(), "renameat2", "a" * 64)
    conn.close()
