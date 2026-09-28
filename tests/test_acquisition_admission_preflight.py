from __future__ import annotations

from pathlib import Path
import os
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest

import app.acquisition_admission_preflight as preflight
import app.acquisition as acquisition_module
import app.acquisition_admission_execution as admission_execution
import app.acquisition_admission_scan as admission_scan
import app.admission_prepublication_review as prepublication_review
import app.prepublication_retirement as retirement
import app.automatic_admission as admission_reconcile
import app.automatic_execution as execution_core
import app.admission as admission_module
from app.acquisition_admission_preflight import acquisition_admission_preview
from app.bindery_client import BinderyClientError
from app.config import settings
from app.db import (
    add_result, create_ebook_acquisition, create_ebook_admission, create_scan,
    ebook_acquisition_by_id, ebook_admission_by_id, finish_scan, init_local_db, local_conn,
    note_ebook_acquisition_admission_blocked, result_by_id,
    update_ebook_acquisition, update_ebook_admission,
)
from app.observe import _acquisition_decisions, _admission_decisions
from app.recovery_planner import (
    promote_due_recovery_retries, record_recovery_plans, recovery_plan_by_id,
)
from app.scan_safety import require_quiescent_admissions
from app.staging import verify_staged_ebook


class FakeClient:
    def __init__(self):
        self.queue_status = "importExternal"
        self.registered = False
        self.scan_attempts = 0

    def scan_library(self):
        self.scan_attempts += 1

    def list_queue(self):
        return {"items": [{
            "id": 77, "bookId": 101, "title": "Review Fixture release",
            "protocol": "usenet", "status": self.queue_status,
        }], "partial": False}

    def get_book(self, book_id):
        return {
            "id": book_id, "title": "Review Fixture", "authorName": "Fixture Author",
            "ebookFilePath": "/data/media/books/Review Fixture.epub" if self.registered else "",
            "bookFiles": [{
                "format": "ebook", "path": "/data/media/books/Review Fixture.epub",
            }] if self.registered else [],
        }

    def get_setting(self, key):
        return {
            "import.mode": "external",
            "import.drop_folder": "/data/bookguard-staging",
            "import.drop_layout": "flat",
            "import.drop_link_mode": "copy",
        }[key]


def _write_epub(path: Path) -> None:
    container = (
        '<?xml version="1.0"?><container '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    package = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>Review Fixture</dc:title><dc:creator>Fixture Author</dc:creator>'
        '<dc:identifier>review-acceptance</dc:identifier></metadata>'
        '<manifest><item id="chapter" href="chapter.xhtml" '
        'media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="chapter"/></spine></package>'
    )
    body = "Review Fixture by Fixture Author. " + ("A fictional passage. " * 100)
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<h1>Review Fixture</h1><p>by Fixture Author</p><p>'
        + body + "</p></body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    books = tmp_path / "books"
    admission = tmp_path / "admission"
    quarantine = tmp_path / "quarantine"
    for path in (staging, books, admission, quarantine):
        path.mkdir()
    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ROOT", str(admission))
    monkeypatch.setenv("BOOKGUARD_ADMISSION_BINDERY_ROOT", "/data/media/books")
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/data/bookguard-staging")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ENABLED", "true")
    monkeypatch.setattr(settings, "allow_actions", True)
    monkeypatch.setattr(settings, "ebook_root", str(books))
    monkeypatch.setattr(settings, "ebook_bindery_prefix", "/data/media/books")
    monkeypatch.setattr(settings, "quarantine_root", str(quarantine))
    monkeypatch.setattr(settings, "config_dir", str(tmp_path / "config"))
    init_local_db()
    create_scan("review-preflight-scan", 1)
    add_result("review-preflight-scan", {
        "file_id": 501, "book_id": 101, "author": "Fixture Author",
        "title": "Review Fixture", "format": "ebook",
        "stored_path": "/data/media/books/Review Fixture.epub",
        "local_path": str(books / "Review Fixture.epub"),
        "classification": "REVIEW", "risk_score": 50,
        "reason_code": "REVIEW_FIXTURE", "reasons": ["fixture"], "metadata": {},
    })
    finish_scan("review-preflight-scan")
    with local_conn() as conn:
        result = dict(conn.execute(
            "SELECT * FROM scan_results WHERE scan_id='review-preflight-scan'"
        ).fetchone())
    client = FakeClient()
    staged = staging / "Review Fixture.epub"
    _write_epub(staged)
    verification = verify_staged_ebook(101, staged.name, client)
    assert verification["safeToAdmit"] is True
    acquisition_id = create_ebook_acquisition(result, {
        "guid": "review-guid", "title": "Review Fixture release",
        "indexerName": "Fixture", "protocol": "usenet",
    })
    update_ebook_acquisition(
        acquisition_id, "verified", queue_id=77, queue_status="importexternal",
        staged_relative_path=staged.name, staged_sha256=verification["sha256"],
        verification=verification,
    )
    with local_conn() as conn:
        record_recovery_plans(conn, _acquisition_decisions(conn, 100))
        conn.commit()
    return acquisition_id, client, staged, admission / staged.name


def _assert_no_admission(destination):
    with local_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ebook_admissions").fetchone()[0] == 0
    assert not destination.exists()


def test_preview_proves_current_evidence_without_publication(prepared):
    acquisition_id, client, staged, destination = prepared
    before = staged.read_bytes()

    preview = acquisition_admission_preview(acquisition_id, client)

    assert preview["ok"] is True
    assert preview["readOnly"] is True
    assert preview["admissionAttempted"] is False
    assert staged.read_bytes() == before
    _assert_no_admission(destination)


@pytest.mark.parametrize("change,reason", [
    ("changed_bytes", "STAGED_BYTES_UNPROVEN"),
    ("unfinished_queue", "QUEUE_HANDOFF_UNPROVEN"),
    ("registered_book", "BINDERY_BOOK_CHANGED"),
    ("occupied_destination", "DEPENDENCY_OR_IDENTITY_UNPROVEN"),
])
def test_preview_blocks_changed_boundary_without_publication(prepared, change, reason):
    acquisition_id, client, staged, destination = prepared
    if change == "changed_bytes":
        staged.write_bytes(staged.read_bytes() + b"changed")
    elif change == "unfinished_queue":
        client.queue_status = "downloading"
    elif change == "registered_book":
        client.registered = True
    else:
        destination.write_bytes(b"occupied")

    preview = acquisition_admission_preview(acquisition_id, client)

    assert preview["ok"] is False
    assert preview["reasonCode"] == reason
    assert preview["admissionAttempted"] is False
    with local_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ebook_admissions").fetchone()[0] == 0


def test_preview_blocks_same_bytes_replaced_during_verification(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    original = preflight.verify_staged_ebook

    def replace_source(*args):
        verified = original(*args)
        replacement = staged.with_suffix(".replacement")
        replacement.write_bytes(staged.read_bytes())
        os.replace(replacement, staged)
        return verified

    monkeypatch.setattr(preflight, "verify_staged_ebook", replace_source)
    preview = acquisition_admission_preview(acquisition_id, client)

    assert preview["reasonCode"] == "STAGED_BYTES_CHANGED"
    _assert_no_admission(destination)


@pytest.mark.parametrize("change", ["new_admission", "superseded_plan", "changed_acquisition"])
def test_preview_blocks_durable_change_during_verification(
    prepared, monkeypatch, change,
):
    acquisition_id, client, staged, destination = prepared
    original = preflight.verify_staged_ebook

    def change_durable_state(*args):
        verified = original(*args)
        with local_conn() as conn:
            if change == "new_admission":
                conn.execute(
                    """INSERT INTO ebook_admissions(
                         result_id, scan_id, book_id, staged_relative_path,
                         stored_path, local_path, status, created_at, updated_at
                       ) SELECT result_id, scan_id, book_id, staged_relative_path,
                         '/data/media/books/Review Fixture.epub',
                         '/books/Review Fixture.epub', 'preparing',
                         datetime('now'), datetime('now')
                         FROM ebook_acquisitions WHERE id=?""",
                    (acquisition_id,),
                )
            elif change == "superseded_plan":
                conn.execute(
                    "UPDATE recovery_plans SET state='superseded' WHERE subject_id=?",
                    (str(acquisition_id),),
                )
            else:
                conn.execute(
                    "UPDATE ebook_acquisitions SET queue_status='downloading' WHERE id=?",
                    (acquisition_id,),
                )
            conn.commit()
        return verified

    monkeypatch.setattr(preflight, "verify_staged_ebook", change_durable_state)
    preview = acquisition_admission_preview(acquisition_id, client)

    assert preview["reasonCode"] == "DURABLE_STATE_CHANGED"
    assert not destination.exists()


def _admission_plan(acquisition_id):
    with local_conn() as conn:
        row = conn.execute(
            "SELECT id FROM recovery_plans WHERE subject_id=? ORDER BY id DESC LIMIT 1",
            (str(acquisition_id),),
        ).fetchone()
    return recovery_plan_by_id(int(row["id"]))


def test_live_admission_boundary_requires_exact_plan(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    plan = _admission_plan(acquisition_id)
    boundary = admission_execution.EXECUTOR.revalidate(plan, plan["steps"][1])

    assert boundary["ok"] is True
    assert boundary["queueId"] == 77
    assert len(boundary["sourceIdentity"]) == 5
    assert boundary["stagedSha256"] == ebook_acquisition_by_id(acquisition_id)["staged_sha256"]
    _assert_no_admission(destination)


def test_live_admission_rechecks_before_guarded_primitive(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    plan = _admission_plan(acquisition_id)
    boundary = admission_execution.EXECUTOR.revalidate(plan, plan["steps"][1])
    staged.write_bytes(staged.read_bytes() + b"changed")
    monkeypatch.setattr(
        admission_execution, "admit_ebook_acquisition",
        lambda *_: pytest.fail("guarded admission must not be called"),
    )

    with pytest.raises(admission_execution.core.AutomaticExecutionBlocked):
        admission_execution.EXECUTOR.execute(plan, plan["steps"][1], boundary)
    _assert_no_admission(destination)


def _seed_published_admission(acquisition_id, staged, destination):
    acquisition = ebook_acquisition_by_id(acquisition_id)
    result = result_by_id(int(acquisition["result_id"]))
    admission_id = create_ebook_admission(result, staged.name)
    destination.write_bytes(staged.read_bytes())
    update_ebook_admission(
        admission_id, "published",
        staged_sha256=acquisition["staged_sha256"],
        publication_method="private-snapshot-link",
        verification={
            "safeToAdmit": True, "sha256": acquisition["staged_sha256"],
        },
    )
    return admission_id


def _failed_prepublication_review(prepared):
    acquisition_id, client, staged, destination = prepared
    acquisition = ebook_acquisition_by_id(acquisition_id)
    result = result_by_id(int(acquisition["result_id"]))
    admission_id = create_ebook_admission(result, staged.name)
    update_ebook_admission(
        admission_id, "failed", failure_stage="before_publication",
        error="injected failure before publication",
    )
    with local_conn() as conn:
        record_recovery_plans(conn, _admission_decisions(conn, 100))
        conn.commit()
    return admission_id


def test_prepublication_review_proves_only_current_exact_handoff(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    admission_id = _failed_prepublication_review(prepared)
    monkeypatch.setattr(prepublication_review, "_exact_ebook_associations", lambda _: [])

    proof = prepublication_review.prepublication_failure_preview(admission_id, client)

    assert proof["ok"] is True
    assert proof["readOnly"] is True
    assert proof["publicationAttempted"] is False
    assert proof["admissionId"] == admission_id
    assert proof["acquisitionId"] == acquisition_id
    assert proof["stagedSha256"] == ebook_acquisition_by_id(acquisition_id)["staged_sha256"]
    assert not destination.exists()
    assert ebook_admission_by_id(admission_id)["status"] == "failed"


@pytest.mark.parametrize("change", [
    "destination", "staged_bytes", "book_registered", "queue_changed",
    "competing_journal", "unknown_stage", "existing_owner",
])
def test_prepublication_review_blocks_changed_evidence(prepared, monkeypatch, change):
    acquisition_id, client, staged, destination = prepared
    admission_id = _failed_prepublication_review(prepared)
    monkeypatch.setattr(prepublication_review, "_exact_ebook_associations", lambda _: [])
    if change == "destination":
        destination.write_bytes(b"occupied")
    elif change == "staged_bytes":
        staged.write_bytes(staged.read_bytes() + b"changed")
    elif change == "book_registered":
        client.registered = True
    elif change == "queue_changed":
        client.queue_status = "downloading"
    elif change == "competing_journal":
        acquisition = ebook_acquisition_by_id(acquisition_id)
        result = result_by_id(int(acquisition["result_id"]))
        create_ebook_admission(result, staged.name)
    elif change == "unknown_stage":
        update_ebook_admission(admission_id, "failed", failure_stage="uncertain")
    else:
        monkeypatch.setattr(
            prepublication_review, "_exact_ebook_associations",
            lambda _: [{"book_id": 101}],
        )

    proof = prepublication_review.prepublication_failure_preview(admission_id, client)

    assert proof["ok"] is False
    assert proof["publicationAttempted"] is False
    assert ebook_admission_by_id(admission_id)["status"] == "failed"


def _prepublication_plan(admission_id):
    with local_conn() as conn:
        row = conn.execute(
            """SELECT id FROM recovery_plans WHERE subject_kind='admission'
               AND subject_id=? ORDER BY id DESC LIMIT 1""",
            (str(admission_id),),
        ).fetchone()
    return recovery_plan_by_id(int(row["id"]))


def test_retirement_requires_separate_allowlist_and_fresh_admission_plan(
    prepared, monkeypatch,
):
    acquisition_id, client, staged, destination = prepared
    old_signature = _admission_plan(acquisition_id)["signature"]
    admission_id = _failed_prepublication_review(prepared)
    monkeypatch.setattr(prepublication_review, "_exact_ebook_associations", lambda _: [])
    monkeypatch.setattr(retirement, "BinderyClient", lambda: client)
    monkeypatch.setattr(execution_core, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic",
        automatic_action_allowlist=("retire_proven_prepublication_failure",),
    ))
    retirement.register_executor()
    plan = _prepublication_plan(admission_id)
    assert [step["code"] for step in plan["steps"]] == [
        "review_failed_admission", "review_current_handoff",
        "retire_proven_prepublication_failure",
    ]
    outcome = retirement.run_prepublication_retirement_cycle(plan)
    assert outcome["state"] == "executed"
    assert outcome["externalMutationAttempted"] is False
    assert ebook_admission_by_id(admission_id)["status"] == "retired_before_publication"
    assert ebook_admission_by_id(admission_id)["error"] == "injected failure before publication"
    assert not destination.exists()
    assert client.scan_attempts == 0

    with local_conn() as conn:
        record_recovery_plans(conn, _admission_decisions(conn, 100))
        record_recovery_plans(conn, _acquisition_decisions(conn, 100))
        conn.commit()
    fresh_plan = _admission_plan(acquisition_id)
    assert fresh_plan["signature"] != old_signature
    assert fresh_plan["planKind"] == "PREPARE_ACQUISITION_ADMISSION"
    assert acquisition_admission_preview(acquisition_id, client)["ok"] is True
    assert ebook_admission_by_id(admission_id)["status"] == "retired_before_publication"


@pytest.mark.parametrize("change", ["destination", "staged_bytes", "queue_changed"])
def test_retirement_blocks_stale_proof(prepared, monkeypatch, change):
    acquisition_id, client, staged, destination = prepared
    admission_id = _failed_prepublication_review(prepared)
    monkeypatch.setattr(prepublication_review, "_exact_ebook_associations", lambda _: [])
    monkeypatch.setattr(retirement, "BinderyClient", lambda: client)
    if change == "destination":
        destination.write_bytes(b"occupied")
    elif change == "staged_bytes":
        staged.write_bytes(staged.read_bytes() + b"changed")
    else:
        client.queue_status = "downloading"
    result = retirement.run_prepublication_retirement_cycle(_prepublication_plan(admission_id))
    assert result["state"] == "blocked"
    assert ebook_admission_by_id(admission_id)["status"] == "failed"
    assert client.scan_attempts == 0


@pytest.mark.parametrize("retired_before_restart", [False, True])
def test_interrupted_retirement_never_retries_local_transition(
    prepared, monkeypatch, retired_before_restart,
):
    _, client, _, destination = prepared
    admission_id = _failed_prepublication_review(prepared)
    monkeypatch.setattr(prepublication_review, "_exact_ebook_associations", lambda _: [])
    monkeypatch.setattr(retirement, "BinderyClient", lambda: client)
    monkeypatch.setattr(execution_core, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic",
        automatic_action_allowlist=("retire_proven_prepublication_failure",),
    ))
    retirement.register_executor()
    plan = _prepublication_plan(admission_id)
    for index in (0, 1):
        plan = execution_core.record_recovery_step_success(int(plan["id"]), index)
    step = plan["steps"][2]
    boundary = retirement.EXECUTOR.revalidate(plan, step)
    execution_core._require_fresh_boundary(plan, boundary)
    execution_core._record(
        plan, "retire_proven_prepublication_failure", 2, "running",
        boundary=boundary, increment_attempt=True,
    )
    if retired_before_restart:
        retirement.EXECUTOR.execute(plan, step, boundary)
        with local_conn() as conn:
            record_recovery_plans(conn, _admission_decisions(conn, 100))
            conn.commit()
        assert recovery_plan_by_id(int(plan["id"]))["state"] == "ready"

    outcome = retirement.run_prepublication_retirement_cycle(
        recovery_plan_by_id(int(plan["id"])),
    )
    assert outcome["state"] == ("reconciled" if retired_before_restart else "blocked")
    assert ebook_admission_by_id(admission_id)["status"] == (
        "retired_before_publication" if retired_before_restart else "failed"
    )
    assert not destination.exists()
    assert client.scan_attempts == 0
    if retired_before_restart:
        assert outcome["execution"]["attemptCount"] == 1
        assert outcome["execution"]["externalResult"]["reconciledAfterRestart"] is True
        assert recovery_plan_by_id(int(plan["id"]))["state"] == "completed"


def test_interrupted_retirement_refuses_competing_journal(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    admission_id = _failed_prepublication_review(prepared)
    monkeypatch.setattr(prepublication_review, "_exact_ebook_associations", lambda _: [])
    monkeypatch.setattr(retirement, "BinderyClient", lambda: client)
    plan = _prepublication_plan(admission_id)
    for index in (0, 1):
        plan = execution_core.record_recovery_step_success(int(plan["id"]), index)
    boundary = retirement.EXECUTOR.revalidate(plan, plan["steps"][2])
    retirement.EXECUTOR.execute(plan, plan["steps"][2], boundary)
    acquisition = ebook_acquisition_by_id(acquisition_id)
    create_ebook_admission(result_by_id(int(acquisition["result_id"])), staged.name)

    with pytest.raises(execution_core.AutomaticExecutionBlocked):
        retirement.EXECUTOR.reconcile_uncertain(
            plan, plan["steps"][2], {"state": "running", "boundary": boundary},
        )
    assert ebook_admission_by_id(admission_id)["status"] == "retired_before_publication"
    assert not destination.exists()
    assert client.scan_attempts == 0


def test_retired_journal_scan_guard_only_exempts_same_published_path(prepared):
    acquisition_id, _, staged, destination = prepared
    old_id = _failed_prepublication_review(prepared)
    update_ebook_admission(old_id, "retired_before_publication")
    new_id = _seed_published_admission(acquisition_id, staged, destination)
    require_quiescent_admissions(new_id)
    with local_conn() as conn:
        conn.execute(
            "UPDATE ebook_admissions SET stored_path=? WHERE id=?",
            ("/data/media/books/Other Fixture.epub", old_id),
        )
        conn.commit()
    destination.with_name("Other Fixture.epub").write_bytes(b"unvetted")
    with pytest.raises(admission_module.AdmissionSafetyError):
        require_quiescent_admissions(new_id)


def _scan_plan(acquisition_id, staged, destination):
    original_plan = _admission_plan(acquisition_id)
    original_boundary = admission_execution.EXECUTOR.revalidate(
        original_plan, original_plan["steps"][1],
    )
    admission_id = _seed_published_admission(acquisition_id, staged, destination)
    update_ebook_acquisition(acquisition_id, "admitted", admission_id=admission_id)
    admission_execution.core._record(
        original_plan, "admit_verified_acquisition", 1, "succeeded",
        boundary=original_boundary,
        external_result={
            "admissionId": admission_id, "acquisitionId": acquisition_id,
            "status": "published", "scanRequested": False,
            "stagedSha256": original_boundary["stagedSha256"],
            "publicationMethod": "private-snapshot-link",
        },
    )
    with local_conn() as conn:
        decisions = _admission_decisions(conn, 100)
        record_recovery_plans(conn, decisions)
        conn.commit()
        row = conn.execute(
            "SELECT id FROM recovery_plans WHERE subject_kind='admission' AND subject_id=?",
            (str(admission_id),),
        ).fetchone()
    return admission_id, recovery_plan_by_id(int(row["id"]))


def test_published_acquisition_scan_one_request_and_no_replay(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_scan, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_scan, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic",
        automatic_action_allowlist={"request_published_acquisition_scan"},
        automatic_reacquisition=True, admission_enabled=True,
    ))
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    admission_id, plan = _scan_plan(acquisition_id, staged, destination)
    assert plan["planKind"] == "REQUEST_PUBLISHED_ACQUISITION_SCAN"
    boundary = admission_scan.EXECUTOR.revalidate(plan, plan["steps"][1])
    admission_scan.core._require_fresh_boundary(plan, boundary)
    admission_scan.core._record(
        plan, "request_published_acquisition_scan", 1, "running",
        boundary=boundary, increment_attempt=True,
    )

    result = admission_scan.EXECUTOR.execute(plan, plan["steps"][1], boundary)

    assert result["status"] == "scan_requested"
    assert client.scan_attempts == 1
    assert ebook_admission_by_id(admission_id)["status"] == "scan_requested"
    assert destination.read_bytes() == staged.read_bytes()
    with pytest.raises(admission_scan.core.AutomaticExecutionBlocked):
        admission_scan.EXECUTOR.revalidate(plan, plan["steps"][1])
    assert client.scan_attempts == 1


def test_interrupted_scan_blocks_when_post_outcome_unknown(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_scan, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    _, plan = _scan_plan(acquisition_id, staged, destination)
    boundary = admission_scan.EXECUTOR.revalidate(plan, plan["steps"][1])
    admission_scan.core._record(
        plan, "request_published_acquisition_scan", 1, "running",
        boundary=boundary, increment_attempt=True,
    )

    with pytest.raises(admission_scan.core.AutomaticExecutionBlocked) as error:
        admission_scan.EXECUTOR.reconcile_uncertain(
            plan, plan["steps"][1], {"boundary": boundary, "state": "running"},
        )

    assert error.value.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME"
    assert client.scan_attempts == 0


def test_interrupted_scan_adopts_durable_scan_status_without_post(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_scan, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    admission_id, plan = _scan_plan(acquisition_id, staged, destination)
    boundary = admission_scan.EXECUTOR.revalidate(plan, plan["steps"][1])
    admission_scan.core._record(
        plan, "request_published_acquisition_scan", 1, "running",
        boundary=boundary, increment_attempt=True,
    )
    update_ebook_admission(admission_id, "scan_requested")

    result = admission_scan.EXECUTOR.reconcile_uncertain(
        plan, plan["steps"][1], {"boundary": boundary, "state": "running"},
    )

    assert result["status"] == "scan_requested"
    assert result["reconciledAfterRestart"] is True
    assert client.scan_attempts == 0


def test_scan_blocks_changed_published_bytes_before_post(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_scan, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    _, plan = _scan_plan(acquisition_id, staged, destination)
    boundary = admission_scan.EXECUTOR.revalidate(plan, plan["steps"][1])
    admission_scan.core._record(
        plan, "request_published_acquisition_scan", 1, "running",
        boundary=boundary, increment_attempt=True,
    )
    destination.write_bytes(b"different disposable bytes")

    with pytest.raises(admission_module.AdmissionSafetyError):
        admission_scan.EXECUTOR.execute(plan, plan["steps"][1], boundary)
    assert client.scan_attempts == 0


def test_ambiguous_scan_failure_keeps_durable_claim_and_boundary(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_scan, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    monkeypatch.setattr(admission_scan, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic",
        automatic_action_allowlist={"request_published_acquisition_scan"},
        automatic_reacquisition=True, admission_enabled=True,
    ))
    admission_id, plan = _scan_plan(acquisition_id, staged, destination)
    boundary = admission_scan.EXECUTOR.revalidate(plan, plan["steps"][1])
    execution_core._record(
        plan, "request_published_acquisition_scan", 1, "running",
        boundary=boundary, increment_attempt=True,
    )

    def ambiguous_scan():
        client.scan_attempts += 1
        raise BinderyClientError("Bindery POST /library/scan: HTTP 504")

    monkeypatch.setattr(client, "scan_library", ambiguous_scan)
    with pytest.raises(BinderyClientError):
        admission_scan.EXECUTOR.execute(plan, plan["steps"][1], boundary)
    execution_core._record(plan, "request_published_acquisition_scan", 1, "failed",
                           boundary=boundary, error="HTTP 504")
    execution_core._record(plan, "request_published_acquisition_scan", 1, "blocked",
                           error="Prior scan outcome unknown")

    receipt = execution_core._existing(plan["signature"], "request_published_acquisition_scan", 1)
    assert receipt["boundary"]["admissionId"] == admission_id
    assert receipt["attemptCount"] == 1
    assert ebook_admission_by_id(admission_id)["status"] == "scan_requesting"
    with local_conn() as conn:
        decision = next(item for item in _admission_decisions(conn, 100)
                        if item["subjectId"] == str(admission_id))
    assert decision["decision"] == "attention"
    assert decision["reasonCode"] == "REGISTRATION_SCAN_OUTCOME_UNCERTAIN"
    with pytest.raises(execution_core.AutomaticExecutionBlocked):
        admission_scan.EXECUTOR.revalidate(plan, plan["steps"][1])
    assert client.scan_attempts == 1


def test_global_scan_blocks_other_unresolved_published_admission(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_scan, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    monkeypatch.setattr(admission_scan, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic",
        automatic_action_allowlist={"request_published_acquisition_scan"},
        automatic_reacquisition=True, admission_enabled=True,
    ))
    _, plan = _scan_plan(acquisition_id, staged, destination)
    boundary = admission_scan.EXECUTOR.revalidate(plan, plan["steps"][1])
    with local_conn() as conn:
        result = dict(conn.execute("SELECT * FROM scan_results LIMIT 1").fetchone())
    other_id = create_ebook_admission(result, staged.name)
    update_ebook_admission(other_id, "published", publication_method="private-snapshot-link")
    execution_core._record(
        plan, "request_published_acquisition_scan", 1, "running",
        boundary=boundary, increment_attempt=True,
    )

    with pytest.raises(admission_module.AdmissionSafetyError, match="unresolved admission"):
        admission_scan.EXECUTOR.execute(plan, plan["steps"][1], boundary)
    assert client.scan_attempts == 0


def test_global_scan_blocks_unjournaled_other_destination(prepared):
    acquisition_id, _, staged, destination = prepared
    admission_id = _seed_published_admission(acquisition_id, staged, destination)
    with local_conn() as conn:
        other = dict(conn.execute("SELECT * FROM scan_results LIMIT 1").fetchone())
    other["stored_path"] = "/data/media/books/Other Fixture.epub"
    other["local_path"] = str(destination.with_name("Other Fixture.epub"))
    other_id = create_ebook_admission(other, "Other Fixture.epub")
    destination.with_name("Other Fixture.epub").write_bytes(b"unvetted")

    with pytest.raises(admission_module.AdmissionSafetyError, match=str(other_id)):
        require_quiescent_admissions(admission_id)


def test_pending_registration_waits_then_adopts_without_second_scan(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_scan, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_reconcile, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [
        {"book_id": 101},
    ] if client.registered else [])
    admission_id, scan_plan = _scan_plan(acquisition_id, staged, destination)
    update_ebook_admission(admission_id, "scan_requested")
    with local_conn() as conn:
        record_recovery_plans(conn, _admission_decisions(conn, 100))
        conn.commit()
        row = conn.execute(
            """SELECT id FROM recovery_plans WHERE subject_kind='admission'
               AND subject_id=? AND plan_kind='RECONCILE_ADMISSION'""",
            (str(admission_id),),
        ).fetchone()
    plan = recovery_plan_by_id(int(row["id"]))
    assert scan_plan["planKind"] == "REQUEST_PUBLISHED_ACQUISITION_SCAN"
    waiting = execution_core._run_admission_reconcile_cycle(plan)
    assert waiting["state"] == "retry_wait"
    assert client.scan_attempts == 0

    client.registered = True
    promote_due_recovery_retries(now="2099-01-01T00:00:00+00:00")
    monkeypatch.setattr(execution_core, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic", automatic_action_allowlist=("reconcile_known_admission",),
    ))
    result = execution_core._run_admission_reconcile_cycle(
        recovery_plan_by_id(int(plan["id"])),
    )
    assert result["state"] == "executed"
    assert ebook_admission_by_id(admission_id)["status"] == "registered"
    assert client.scan_attempts == 0


def test_losing_worker_cannot_revert_an_admitted_acquisition(prepared):
    acquisition_id, _, staged, destination = prepared
    admission_id = _seed_published_admission(acquisition_id, staged, destination)
    update_ebook_acquisition(acquisition_id, "admitted", admission_id=admission_id)

    note_ebook_acquisition_admission_blocked(acquisition_id, "Other worker refused publication")

    acquisition = ebook_acquisition_by_id(acquisition_id)
    assert acquisition["status"] == "admitted"
    assert acquisition["admission_id"] == admission_id


def test_interrupted_admission_adopts_only_proven_journal(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    plan = _admission_plan(acquisition_id)
    boundary = admission_execution.EXECUTOR.revalidate(plan, plan["steps"][1])
    admission_id = _seed_published_admission(acquisition_id, staged, destination)

    result = admission_execution.EXECUTOR.reconcile_uncertain(
        plan, plan["steps"][1], {"state": "running", "boundary": boundary},
    )

    assert result["reconciledAfterRestart"] is True
    assert result["externalMutationPerformed"] is False
    assert result["scanRequested"] is False
    assert result["status"] == "published"
    assert ebook_acquisition_by_id(acquisition_id)["admission_id"] == admission_id
    assert destination.read_bytes() == staged.read_bytes()


def test_post_publication_link_failure_is_classified_as_uncertain(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(acquisition_module, "_automation_settings", lambda: SimpleNamespace(
        automatic_reacquisition=True,
    ))
    original_update = acquisition_module.update_ebook_acquisition

    def fail_link(acquisition_id, status, **kwargs):
        if status == "admitted":
            raise sqlite3.OperationalError("injected link failure")
        return original_update(acquisition_id, status, **kwargs)

    monkeypatch.setattr(acquisition_module, "update_ebook_acquisition", fail_link)
    monkeypatch.setattr(
        acquisition_module, "admit_staged_ebook",
        lambda *_args, **_kwargs: {
            "admissionId": _seed_published_admission(acquisition_id, staged, destination),
        },
    )

    with pytest.raises(acquisition_module.AcquisitionPostPublicationUncertain):
        acquisition_module.admit_ebook_acquisition(
            acquisition_id, client, before_publish=lambda _: None, request_scan=False,
        )
    assert ebook_acquisition_by_id(acquisition_id)["status"] == "verified"
    assert destination.read_bytes() == staged.read_bytes()


def test_post_publication_failure_waits_then_adopts_without_replay(prepared, monkeypatch):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    monkeypatch.setattr(execution_core, "load_automation_settings", lambda: SimpleNamespace(
        automation_mode="automatic",
        automatic_action_allowlist=("admit_verified_acquisition",),
    ))
    publications = []

    def publish_then_fail(*_args, **_kwargs):
        publications.append(_seed_published_admission(acquisition_id, staged, destination))
        raise acquisition_module.AcquisitionPostPublicationUncertain("link write failed")

    monkeypatch.setattr(admission_execution, "admit_ebook_acquisition", publish_then_fail)
    plan = _admission_plan(acquisition_id)
    waiting = admission_execution.run_verified_admission_cycle(plan)
    receipt = execution_core._existing(
        plan["signature"], "admit_verified_acquisition", 1,
    )
    assert waiting["state"] == "waiting"
    assert waiting["reasonCode"] == "POST_EFFECT_UNCERTAIN"
    assert waiting["plan"]["state"] == "ready"
    assert receipt["state"] == "running"
    assert receipt["boundary"]["stagedSha256"] == ebook_acquisition_by_id(
        acquisition_id,
    )["staged_sha256"]

    monkeypatch.setattr(
        admission_execution, "admit_ebook_acquisition",
        lambda *_args, **_kwargs: pytest.fail("publication must not repeat"),
    )
    adopted = admission_execution.run_verified_admission_cycle(
        recovery_plan_by_id(int(plan["id"])),
    )
    assert adopted["state"] == "reconciled"
    assert len(publications) == 1
    assert ebook_acquisition_by_id(acquisition_id)["admission_id"] == publications[0]
    assert destination.read_bytes() == staged.read_bytes()
    assert client.scan_attempts == 0


@pytest.mark.parametrize("change", ["missing_journal", "changed_destination", "replaced_source"])
def test_interrupted_admission_never_replays_unproven_publication(
    prepared, monkeypatch, change,
):
    acquisition_id, client, staged, destination = prepared
    monkeypatch.setattr(admission_execution, "BinderyClient", lambda: client)
    monkeypatch.setattr(admission_module, "_exact_ebook_associations", lambda _: [])
    plan = _admission_plan(acquisition_id)
    boundary = admission_execution.EXECUTOR.revalidate(plan, plan["steps"][1])
    if change != "missing_journal":
        _seed_published_admission(acquisition_id, staged, destination)
    if change == "changed_destination":
        destination.write_bytes(b"wrong bytes")
    elif change == "replaced_source":
        replacement = staged.with_suffix(".replacement")
        replacement.write_bytes(staged.read_bytes())
        os.replace(replacement, staged)
    monkeypatch.setattr(
        admission_execution, "admit_ebook_acquisition",
        lambda *_: pytest.fail("publication must never be replayed"),
    )

    with pytest.raises(admission_execution.core.AutomaticExecutionBlocked) as exc:
        admission_execution.EXECUTOR.reconcile_uncertain(
            plan, plan["steps"][1], {"state": "running", "boundary": boundary},
        )
    assert exc.value.reason_code == "UNCERTAIN_EXTERNAL_OUTCOME"
    assert ebook_acquisition_by_id(acquisition_id)["status"] == "verified"
