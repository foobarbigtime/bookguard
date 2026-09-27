"""The bytes BookGuard publishes are the bytes it verified.

Covers the window between the private snapshot's verification and its
no-replace publication, where a same-UID process could rewrite the snapshot,
and the published destination, which must be proven before success is recorded.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import app.acquisition as acquisition_module
import app.acquisition_admission_execution as admission_execution
import app.admission as admission
import app.automatic_execution as execution_core
from app.db import ebook_acquisition_by_id, local_conn, recent_ebook_admissions
from test_acquisition_admission_preflight import _admission_plan, prepared  # noqa: F401
from test_admission import FakeClient, admission_setup  # noqa: F401


def _private_snapshot(destination: Path) -> Path:
    snapshots = list(destination.parent.glob(".bookguard-admission-*/snapshot.epub"))
    assert len(snapshots) == 1
    return snapshots[0]


def _tamper(path: Path) -> None:
    """Rewrite a file in place as a same-UID process could, keeping its size."""
    with path.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        last = handle.read(1)
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes([last[0] ^ 0xFF]))


def test_snapshot_tampered_during_live_authorization_is_never_published(
    admission_setup,  # noqa: F811
):
    setup = admission_setup
    destination = setup["admission_root"] / setup["relative"]
    tampered = []

    def tamper_during_authorization(_admission_id):
        snapshot = _private_snapshot(destination)
        _tamper(snapshot)
        tampered.append(snapshot)

    with pytest.raises(admission.AdmissionSafetyError, match="changed while it was sealed"):
        admission.admit_staged_ebook(
            setup["result"], setup["staged"].name, FakeClient(),
            before_publish=tamper_during_authorization, request_scan=False,
        )

    assert tampered
    assert not destination.exists()
    assert not list(destination.parent.glob(".bookguard-admission-*"))
    record = recent_ebook_admissions()[0]
    assert record["status"] == "failed"
    assert record["failure_stage"] == "before_publication"
    assert record["publication_method"] is None


def test_snapshot_tampered_after_seal_is_never_published(
    admission_setup, monkeypatch,  # noqa: F811
):
    setup = admission_setup
    destination = setup["admission_root"] / setup["relative"]
    seal = admission._seal_snapshot

    def seal_then_tamper(snapshot, expected_hash):
        sealed = seal(snapshot, expected_hash)
        _tamper(snapshot)
        return sealed

    monkeypatch.setattr(admission, "_seal_snapshot", seal_then_tamper)
    with pytest.raises(admission.AdmissionSafetyError, match="changed before publication"):
        admission.admit_staged_ebook(
            setup["result"], setup["staged"].name, FakeClient(), request_scan=False,
        )

    assert not destination.exists()
    record = recent_ebook_admissions()[0]
    assert record["status"] == "failed"
    assert record["failure_stage"] == "before_publication"


def test_snapshot_replaced_by_symlink_after_seal_is_never_published(
    admission_setup, monkeypatch, tmp_path,  # noqa: F811
):
    setup = admission_setup
    destination = setup["admission_root"] / setup["relative"]
    decoy = tmp_path / "decoy.epub"
    decoy.write_bytes(setup["staged"].read_bytes())
    seal = admission._seal_snapshot

    def seal_then_swap(snapshot, expected_hash):
        sealed = seal(snapshot, expected_hash)
        snapshot.unlink()
        snapshot.symlink_to(decoy)
        return sealed

    monkeypatch.setattr(admission, "_seal_snapshot", seal_then_swap)
    with pytest.raises(admission.AdmissionSafetyError, match="changed before publication"):
        admission.admit_staged_ebook(
            setup["result"], setup["staged"].name, FakeClient(), request_scan=False,
        )

    assert not destination.exists()
    assert recent_ebook_admissions()[0]["failure_stage"] == "before_publication"


def test_published_bytes_are_proven_before_success_is_recorded(
    admission_setup, monkeypatch,  # noqa: F811
):
    setup = admission_setup
    destination = setup["admission_root"] / setup["relative"]
    publish = admission._publish_no_replace

    def publish_then_tamper(snapshot, target):
        method = publish(snapshot, target)
        _tamper(target)
        return method

    monkeypatch.setattr(admission, "_publish_no_replace", publish_then_tamper)
    client = FakeClient()
    with pytest.raises(admission.AdmissionSafetyError, match="could not be proven identical"):
        admission.admit_staged_ebook(setup["result"], setup["staged"].name, client)

    record = recent_ebook_admissions()[0]
    # The file exists, so the journal says so, with the error kept for review.
    assert record["status"] == "published"
    assert record["publication_method"] == "renameat2"
    assert "does not match the verified bytes" in record["error"]
    assert destination.is_file()
    assert client.scan_requests == 0
    assert hashlib.sha256(destination.read_bytes()).hexdigest() != record["staged_sha256"]


def test_live_admission_blocks_when_published_bytes_are_unproven(
    prepared, monkeypatch,  # noqa: F811
):
    """Through the real E4 executor: tampered library bytes block the plan."""
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "automatic")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST", "admit_verified_acquisition")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    for module in (admission_execution, admission, acquisition_module):
        monkeypatch.setattr(module, "BinderyClient", lambda: client, raising=False)
    publish = admission._publish_no_replace

    def publish_then_tamper(snapshot, target):
        method = publish(snapshot, target)
        _tamper(target)
        return method

    monkeypatch.setattr(admission, "_publish_no_replace", publish_then_tamper)
    admission_execution.register_executor()
    plan = _admission_plan(acquisition_id)
    outcome = admission_execution.run_verified_admission_cycle(plan)

    assert outcome["state"] == "blocked"
    assert outcome["reasonCode"] == "EXECUTION_FAILED"
    assert outcome["plan"]["state"] == "blocked"
    receipt = execution_core._existing(plan["signature"], "admit_verified_acquisition", 1)
    assert receipt["state"] == "failed"
    assert "could not be proven identical" in receipt["error"]
    assert ebook_acquisition_by_id(acquisition_id)["status"] == "verified"
    with local_conn() as conn:
        journal = dict(conn.execute("SELECT * FROM ebook_admissions").fetchone())
    assert journal["status"] == "published"
    assert "does not match the verified bytes" in journal["error"]
    assert destination.read_bytes() != staged.read_bytes()
    assert client.scan_attempts == 0
