import hashlib
from pathlib import Path
import zipfile

import pytest

from app import (
    acquisition, alternate_candidate, alternate_selection, automatic_alternate,
    automatic_runner, scan_guard,
)
from app.db import (
    create_ebook_acquisition,
    ebook_acquisition_by_id,
    ebook_replacement_for_acquisition,
    init_local_db,
    recent_ebook_acquisitions,
    update_ebook_acquisition,
)
from app.observe import run_observe_cycle
from app.recovery_planner import record_recovery_step_success, recovery_plan_by_id


class FakeClient:
    def __init__(self):
        self.queue = {"items": [], "partial": False}
        self.history = {"items": []}
        self.auto_grab = "false"
        self.grabs = []
        self.removed_queue_items = []
        self.registered = False
        self.candidate = {
            "guid": "safe-guid",
            "title": "Ann Patchett - Bel Canto retail epub",
            "nzbUrl": "https://indexer.invalid/safe.nzb",
            "size": 2048,
            "approved": True,
            "mediaType": "ebook",
            "protocol": "usenet",
            "indexerId": 4,
            "indexerName": "Test Indexer",
        }

    def get_setting(self, key):
        return {
            "import.mode": "external",
            "import.drop_folder": "/data/bookguard-staging",
            "import.drop_layout": "flat",
            "import.drop_link_mode": "copy",
            "autoGrab.enabled": self.auto_grab,
        }[key]

    def list_queue(self):
        return self.queue

    def get_book(self, book_id):
        path = (
            "/data/media/books/Ann Patchett/Bel Canto (2001)/"
            "Bel Canto - Ann Patchett.epub"
        )
        return {
            "id": book_id,
            "title": "Bel Canto",
            "author": {"name": "Ann Patchett"},
            "ebookFilePath": path if self.registered else "",
            "bookFiles": (
                [{"format": "ebook", "path": path}]
                if self.registered
                else []
            ),
        }

    def search_book(self, book_id):
        return {"results": [self.candidate]}

    def list_history(self, book_id, event_type=None, limit=100):
        return self.history

    def grab(self, book_id, candidate):
        self.grabs.append((book_id, candidate["guid"]))
        return {"queueItem": {"id": 77}}

    def remove_queue_item(
        self,
        queue_id,
        *,
        remove_from_client=False,
        delete_files=False,
    ):
        self.removed_queue_items.append(
            (queue_id, remove_from_client, delete_files)
        )
        self.queue["items"] = [
            item
            for item in self.queue["items"]
            if int(item.get("id") or 0) != int(queue_id)
        ]


def _write_epub(path: Path, title: str, author: str) -> None:
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    package = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:identifier>acquisition-test</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    body = f"{title} by {author}. " + ("A fictional passage. " * 100)
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f"<h1>{title}</h1><p>by {author}</p><p>{body}</p>"
        "</body></html>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


@pytest.fixture
def acquisition_setup(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    books = tmp_path / "books"
    quarantine = tmp_path / "quarantine"
    admission = tmp_path / "admission"
    config = tmp_path / "config"
    relative = Path("Ann Patchett/Bel Canto (2001)/Bel Canto - Ann Patchett.epub")
    for path in (staging, books, quarantine, admission, config):
        path.mkdir()
    (books / relative.parent).mkdir(parents=True)
    (admission / relative.parent).mkdir(parents=True)

    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.setenv(
        "BOOKGUARD_BINDERY_DROP_FOLDER",
        "/data/bookguard-staging",
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ENABLED", "true")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ROOT", str(admission))
    monkeypatch.setenv(
        "BOOKGUARD_ADMISSION_BINDERY_ROOT",
        "/data/media/books",
    )
    monkeypatch.setattr(acquisition.settings, "allow_actions", True)
    monkeypatch.setattr(acquisition.settings, "ebook_root", str(books))
    monkeypatch.setattr(
        acquisition.settings,
        "ebook_bindery_prefix",
        "/data/media/books",
    )
    monkeypatch.setattr(
        acquisition.settings,
        "audiobook_root",
        str(tmp_path / "audiobooks"),
    )
    monkeypatch.setattr(
        acquisition.settings,
        "quarantine_root",
        str(quarantine),
    )
    monkeypatch.setattr(acquisition.settings, "config_dir", str(config))
    monkeypatch.setattr(
        scan_guard,
        "latest_scan",
        lambda: {"id": "scan-1", "status": "complete"},
    )
    init_local_db()

    result = {
        "id": 17,
        "scan_id": "scan-1",
        "file_id": 22,
        "book_id": 42,
        "title": "Bel Canto",
        "author": "Ann Patchett",
        "format": "ebook",
        "classification": "REVIEW",
        "stored_path": str(Path("/data/media/books") / relative),
        "local_path": str(books / relative),
    }
    monkeypatch.setattr(
        acquisition,
        "result_by_id",
        lambda result_id: result if result_id == 17 else None,
    )
    return {
        "staging": staging,
        "result": result,
        "client": FakeClient(),
    }


def test_result_and_book_translates_stale_shared_scan_guard(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    monkeypatch.setattr(
        scan_guard,
        "latest_scan",
        lambda: {"id": "newer-scan", "status": "complete"},
    )

    with pytest.raises(
        acquisition.AcquisitionSafetyError,
        match="acquisition result is not from the latest completed scan",
    ):
        acquisition._result_and_book(setup["result"], setup["client"])


def test_readiness_requires_actions_and_automatic_opt_in(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    monkeypatch.setattr(acquisition.settings, "allow_actions", False)
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "false")

    result = acquisition.acquisition_readiness(setup["client"])

    assert result["ready"] is False
    assert "actionsEnabled" in result["blockers"]
    assert "automaticReacquisitionEnabled" in result["blockers"]


def test_readiness_requires_disabled_auto_grab_and_complete_idle_queue(
    acquisition_setup,
):
    client = acquisition_setup["client"]
    client.auto_grab = "true"
    client.queue = {
        "items": [{"id": 8, "status": "downloading"}],
        "partial": True,
    }

    result = acquisition.acquisition_readiness(client)

    assert result["ready"] is False
    assert "binderyAutoGrabDisabled" in result["blockers"]
    assert "binderyQueueComplete" in result["blockers"]
    assert "binderyQueueIdle" in result["blockers"]


def test_start_revalidates_and_records_one_explicit_grab(acquisition_setup):
    setup = acquisition_setup

    result = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )

    record = result["acquisition"]
    assert record["status"] == "queued"
    assert record["queue_id"] == 77
    assert record["candidate_guid"] == "safe-guid"
    assert record["grab_response"] == {
        "id": 77,
        "bookId": None,
        "title": None,
        "status": None,
        "protocol": None,
    }
    assert setup["client"].grabs == [(42, "safe-guid")]


def test_start_rejects_release_that_fails_identity_gate(acquisition_setup):
    setup = acquisition_setup
    setup["client"].candidate["title"] = "Different Writer - Other Book epub"

    with pytest.raises(acquisition.AcquisitionSafetyError, match="safety gate"):
        acquisition.start_ebook_acquisition(
            setup["result"],
            "safe-guid",
            setup["client"],
        )

    assert setup["client"].grabs == []
    assert recent_ebook_acquisitions() == []


def test_start_rejects_exact_release_already_imported(acquisition_setup):
    setup = acquisition_setup
    setup["client"].history = {
        "items": [
            {
                "eventType": "grabbed",
                "sourceTitle": setup["client"].candidate["title"],
            },
            {
                "eventType": "bookImported",
                "sourceTitle": setup["client"].candidate["title"],
            },
        ]
    }

    with pytest.raises(acquisition.AcquisitionSafetyError, match="history"):
        acquisition.start_ebook_acquisition(
            setup["result"],
            "safe-guid",
            setup["client"],
        )

    assert setup["client"].grabs == []
    assert recent_ebook_acquisitions() == []


@pytest.mark.parametrize("history", [
    {"items": [], "partial": True},
    {"items": [{"eventType": "other"}] * 200},
    {"items": [None]},
])
def test_start_refuses_incomplete_or_invalid_history(acquisition_setup, history):
    setup = acquisition_setup
    setup["client"].history = history

    with pytest.raises(acquisition.AcquisitionSafetyError, match="history"):
        acquisition.start_ebook_acquisition(
            setup["result"], "safe-guid", setup["client"],
        )

    assert setup["client"].grabs == []
    assert recent_ebook_acquisitions() == []


def test_alternate_preview_checks_explicit_release_without_grab(
    acquisition_setup, monkeypatch,
):
    setup = acquisition_setup
    client = setup["client"]
    failed_id = create_ebook_acquisition(setup["result"], client.candidate)
    update_ebook_acquisition(
        failed_id, "failed",
        error=(
            "Bindery grab failed: Bindery POST /queue/grab returned HTTP 409: "
            "already grabbed: this release has already been imported"
        ),
    )
    monkeypatch.setattr(
        alternate_candidate, "result_by_id", lambda result_id: setup["result"],
    )
    original_title = client.candidate["title"]
    client.candidate = {
        **client.candidate,
        "guid": "alternate-guid",
        "title": "Ann Patchett - Bel Canto revised retail epub",
    }

    preview = alternate_candidate.alternate_candidate_preview(
        failed_id, "alternate-guid", client,
    )

    assert preview["safeForReview"] is True
    assert preview["liveGrabEnabled"] is False
    assert preview["rejectedGuid"] == "safe-guid"
    assert preview["candidate"]["guid"] == "alternate-guid"
    assert preview["candidate"]["title"] != original_title
    assert "nzbUrl" not in preview["candidate"]
    assert client.grabs == []
    assert ebook_acquisition_by_id(failed_id)["status"] == "failed"
    assert len(recent_ebook_acquisitions()) == 1


@pytest.mark.parametrize("case", [
    "same_guid", "same_title", "partial_history", "partial_search",
    "busy_queue", "uncertain_queue", "unsafe_identity",
])
def test_alternate_preview_fails_closed(acquisition_setup, monkeypatch, case):
    setup = acquisition_setup
    client = setup["client"]
    failed_id = create_ebook_acquisition(setup["result"], client.candidate)
    update_ebook_acquisition(
        failed_id, "failed",
        error=(
            "Bindery grab failed: Bindery POST /queue/grab returned HTTP 409: "
            "already grabbed: this release has already been imported"
        ),
        queue_id=77 if case == "uncertain_queue" else None,
    )
    monkeypatch.setattr(
        alternate_candidate, "result_by_id", lambda result_id: setup["result"],
    )
    client.candidate = {
        **client.candidate,
        "guid": "alternate-guid",
        "title": (
            client.candidate["title"] if case == "same_title"
            else "Ann Patchett - Bel Canto revised retail epub"
        ),
    }
    if case == "partial_history":
        client.history = {"items": [], "partial": True}
    if case == "partial_search":
        client.search_book = lambda book_id: {
            "results": [client.candidate], "partial": True,
        }
    if case == "busy_queue":
        client.queue = {"items": [{"id": 9, "status": "downloading"}], "partial": False}
    if case == "unsafe_identity":
        client.candidate["title"] = "Wrong Writer - Wrong Title"

    guid = "safe-guid" if case == "same_guid" else "alternate-guid"
    with pytest.raises(acquisition.AcquisitionSafetyError):
        alternate_candidate.alternate_candidate_preview(failed_id, guid, client)

    assert client.grabs == []
    assert len(recent_ebook_acquisitions()) == 1


def test_alternate_choice_binds_exact_plan_and_payload_without_grab(
    acquisition_setup, monkeypatch,
):
    setup = acquisition_setup
    client = setup["client"]
    failed_id = create_ebook_acquisition(setup["result"], client.candidate)
    update_ebook_acquisition(
        failed_id, "failed",
        error=(
            "Bindery grab failed: Bindery POST /queue/grab returned HTTP 409: "
            "already grabbed: this release has already been imported"
        ),
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
    plan = next(
        item for item in run_observe_cycle()["plans"]
        if item["planKind"] == "SELECT_ALTERNATE_REPLACEMENT"
    )
    monkeypatch.setattr(
        alternate_candidate, "result_by_id", lambda result_id: setup["result"],
    )
    client.candidate = {
        **client.candidate,
        "guid": "alternate-guid",
        "title": "Ann Patchett - Bel Canto revised retail epub",
    }

    selected = alternate_selection.bind_alternate_candidate(
        plan["id"], "alternate-guid", client,
    )
    again = alternate_selection.bind_alternate_candidate(
        plan["id"], "alternate-guid", client,
    )

    assert selected == again
    assert selected["planSignature"] == plan["signature"]
    assert selected["evidenceRevision"] == plan["evidenceRevision"]
    assert selected["currentPlan"] is True
    assert selected["liveGrabEnabled"] is False
    assert selected["candidate"]["guid"] == "alternate-guid"
    assert len(selected["candidateFingerprint"]) == 64
    assert "nzbUrl" not in str(selected)
    assert alternate_selection.alternate_selection_by_acquisition(failed_id) == selected
    assert client.grabs == []
    assert len(recent_ebook_acquisitions()) == 1

    client.candidate["nzbUrl"] = "https://indexer.invalid/changed.nzb"
    with pytest.raises(acquisition.AcquisitionSafetyError, match="already has"):
        alternate_selection.bind_alternate_candidate(plan["id"], "alternate-guid", client)
    assert alternate_selection.alternate_selection_by_acquisition(failed_id) == selected

    update_ebook_acquisition(failed_id, "failed", error="Unknown new failure")
    assert alternate_selection.alternate_selection_by_acquisition(failed_id)[
        "currentPlan"
    ] is False
    with pytest.raises(acquisition.AcquisitionSafetyError, match="stale"):
        alternate_selection.bind_alternate_candidate(plan["id"], "alternate-guid", client)
    assert client.grabs == []


def _selected_alternate_for_live_test(setup, monkeypatch):
    client = setup["client"]
    failed_id = create_ebook_acquisition(setup["result"], client.candidate)
    update_ebook_acquisition(
        failed_id, "failed",
        error=(
            "Bindery grab failed: Bindery POST /queue/grab returned HTTP 409: "
            "already grabbed: this release has already been imported"
        ),
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
    plan = next(
        item for item in run_observe_cycle()["plans"]
        if item["planKind"] == "SELECT_ALTERNATE_REPLACEMENT"
    )
    monkeypatch.setattr(
        alternate_candidate, "result_by_id", lambda result_id: setup["result"],
    )
    monkeypatch.setattr(
        automatic_alternate, "result_by_id", lambda result_id: setup["result"],
    )
    client.candidate = {
        **client.candidate,
        "guid": "alternate-guid",
        "title": "Ann Patchett - Bel Canto revised retail epub",
    }
    alternate_selection.bind_alternate_candidate(plan["id"], "alternate-guid", client)
    preview = alternate_candidate.alternate_candidate_preview
    monkeypatch.setattr(automatic_alternate, "BinderyClient", lambda: client)
    monkeypatch.setattr(
        automatic_alternate, "alternate_candidate_preview",
        lambda acquisition_id, guid, supplied=None: preview(acquisition_id, guid, client),
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "automatic")
    return failed_id, plan, client


def test_guarded_alternate_grabs_once_and_retains_admission_gate(
    acquisition_setup, monkeypatch,
):
    parent_id, plan, client = _selected_alternate_for_live_test(
        acquisition_setup, monkeypatch,
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST", "")
    with pytest.raises(automatic_runner.AutomaticExecutionBlocked) as blocked:
        automatic_runner.run_automatic_cycle()
    assert blocked.value.reason_code == "ACTION_NOT_ALLOWLISTED"
    assert client.grabs == []
    assert ebook_replacement_for_acquisition(parent_id) is None

    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST", "request_alternate_grab")
    executed = automatic_runner.run_automatic_cycle()
    child = ebook_replacement_for_acquisition(parent_id)
    assert executed["state"] == "executed"
    assert executed["externalMutationAttempted"] is True
    assert child["status"] == "queued"
    assert child["candidate_guid"] == "alternate-guid"
    assert child["admission_id"] is None
    assert ebook_acquisition_by_id(parent_id)["status"] == "failed"
    assert client.grabs == [(42, "alternate-guid")]
    assert automatic_runner.run_automatic_cycle()["state"] == "paused"
    assert client.grabs == [(42, "alternate-guid")]


def test_alternate_grab_proves_queue_when_response_omits_id(
    acquisition_setup, monkeypatch,
):
    parent_id, _, client = _selected_alternate_for_live_test(
        acquisition_setup, monkeypatch,
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST", "request_alternate_grab")

    def accepted_without_id(book_id, candidate):
        client.grabs.append((book_id, candidate["guid"]))
        client.queue = {"items": [{
            "id": 77, "bookId": book_id, "title": candidate["title"],
            "protocol": candidate["protocol"], "status": "downloading",
        }], "partial": False}
        return {"accepted": True}

    monkeypatch.setattr(client, "grab", accepted_without_id)
    outcome = automatic_runner.run_automatic_cycle()
    child = ebook_replacement_for_acquisition(parent_id)

    assert outcome["state"] == "executed"
    assert child["status"] == "queued"
    assert child["queue_id"] == 77
    assert child["grab_response"]["id"] == 77
    assert child["admission_id"] is None
    assert client.grabs == [(42, "alternate-guid")]


def test_alternate_grab_blocks_ambiguous_post_grab_queue_without_replay(
    acquisition_setup, monkeypatch,
):
    parent_id, _, client = _selected_alternate_for_live_test(
        acquisition_setup, monkeypatch,
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST", "request_alternate_grab")

    def accepted_with_ambiguous_queue(book_id, candidate):
        client.grabs.append((book_id, candidate["guid"]))
        client.queue = {"items": [{
            "id": number, "bookId": book_id, "title": candidate["title"],
            "protocol": candidate["protocol"], "status": "downloading",
        } for number in (77, 78)], "partial": False}
        return {"accepted": True}

    monkeypatch.setattr(client, "grab", accepted_with_ambiguous_queue)
    outcome = automatic_runner.run_automatic_cycle()
    child = ebook_replacement_for_acquisition(parent_id)

    assert outcome["state"] == "blocked"
    assert "Exactly one current queue item" in outcome["message"]
    assert child["status"] == "grab_requested"
    assert child["queue_id"] is None
    assert client.grabs == [(42, "alternate-guid")]
    automatic_runner.run_automatic_cycle()
    assert client.grabs == [(42, "alternate-guid")]


def test_alternate_queue_proof_rejects_conflicting_ids():
    client = FakeClient()
    client.queue = {"items": [{
        "id": 77, "queueId": 78, "bookId": 42,
        "title": client.candidate["title"], "protocol": "usenet",
        "status": "downloading",
    }], "partial": False}

    with pytest.raises(acquisition.AcquisitionSafetyError, match="identity is unproven"):
        automatic_alternate._prove_queue_after_grab(
            client, 42, client.candidate["title"], "usenet",
        )


def test_interrupted_alternate_adopts_proven_queue_without_second_grab(
    acquisition_setup, monkeypatch,
):
    parent_id, plan, client = _selected_alternate_for_live_test(
        acquisition_setup, monkeypatch,
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST", "request_alternate_grab")
    executor = automatic_alternate._EXECUTOR
    for index in range(3):
        record_recovery_step_success(plan["id"], index)
    ready = recovery_plan_by_id(plan["id"])
    boundary = executor.revalidate(ready, ready["steps"][3])
    child_id = create_ebook_acquisition(
        acquisition_setup["result"], client.candidate,
        replacement_for_acquisition_id=parent_id,
    )
    update_ebook_acquisition(child_id, "grab_requested")
    client.queue = {
        "items": [{
            "id": 77, "bookId": 42, "title": client.candidate["title"],
            "protocol": "usenet", "status": "downloading",
        }],
        "partial": False,
    }
    from app import automatic_execution as core
    core._record(
        ready, "request_alternate_grab", 3, "running",
        boundary=boundary, increment_attempt=True,
    )

    adopted = automatic_runner.run_automatic_cycle()

    assert adopted["state"] == "reconciled"
    assert client.grabs == []
    assert ebook_replacement_for_acquisition(parent_id)["queue_id"] == 77
    assert ebook_replacement_for_acquisition(parent_id)["admission_id"] is None


def test_alternate_grab_refuses_queue_arriving_during_final_search(
    acquisition_setup, monkeypatch,
):
    parent_id, _, client = _selected_alternate_for_live_test(
        acquisition_setup, monkeypatch,
    )
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST", "request_alternate_grab")
    original_search = client.search_book
    searches = 0

    def competing_search(book_id):
        nonlocal searches
        searches += 1
        result = original_search(book_id)
        if searches == 6:
            # The last search succeeds, but another actor now owns the queue.
            client.queue = {"items": [{
                "id": 91, "bookId": 99, "title": "Unrelated release",
                "protocol": "usenet", "status": "downloading",
            }], "partial": False}
        return result

    monkeypatch.setattr(client, "search_book", competing_search)
    outcome = automatic_runner.run_automatic_cycle()

    assert searches == 6
    assert outcome["state"] == "blocked"
    assert "binderyQueueIdle" in outcome["message"]
    assert client.grabs == []
    assert ebook_replacement_for_acquisition(parent_id) is None


def test_reconcile_verifies_exactly_one_staged_ebook(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "importExternal"}],
        "partial": False,
    }

    observed = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    result = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert observed["acquisition"]["status"] == "staging_observed"
    record = result["acquisition"]
    assert record["status"] == "verified"
    assert record["queue_status"] == "importexternal"
    assert record["staged_relative_path"] == staged.name
    assert record["staged_sha256"] == hashlib.sha256(staged.read_bytes()).hexdigest()
    assert record["verification"]["safeToAdmit"] is True
    assert staged.is_file()


def test_reconcile_refuses_ambiguous_staging(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    _write_epub(setup["staging"] / "first.epub", "Bel Canto", "Ann Patchett")
    _write_epub(setup["staging"] / "second.epub", "Bel Canto", "Ann Patchett")

    result = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert result["ok"] is False
    assert result["acquisition"]["status"] == "review_required"
    assert len(result["stagedFiles"]) == 2


def test_operator_can_retry_review_required_staged_verification(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "importExternal"}],
        "partial": False,
    }

    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    monkeypatch.setattr(
        acquisition,
        "verify_staged_ebook",
        lambda *args, **kwargs: {
            "relativePath": staged.name,
            "safeToAdmit": False,
            "verdict": "INSUFFICIENT_EVIDENCE",
            "confidence": 70,
            "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        },
    )
    reviewed = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    assert reviewed["acquisition"]["status"] == "review_required"

    monkeypatch.setattr(
        acquisition,
        "verify_staged_ebook",
        lambda *args, **kwargs: {
            "relativePath": staged.name,
            "safeToAdmit": True,
            "verdict": "VERIFIED_CORRECT",
            "confidence": 99,
            "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        },
    )
    retried = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert retried["ok"] is True
    assert retried["acquisition"]["status"] == "verified"
    assert retried["acquisition"]["staged_relative_path"] == staged.name
    assert staged.is_file()


def test_reconcile_records_failed_bindery_download(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    setup["client"].queue = {
        "items": [{
            "id": 77,
            "bookId": 42,
            "status": "failed",
            "errorMessage": "download client rejected it",
        }],
        "partial": False,
    }

    result = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert result["acquisition"]["status"] == "failed"
    assert "download client" in result["acquisition"]["error"]


def test_reconcile_does_not_verify_before_external_handoff(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "downloading"}],
        "partial": False,
    }

    response = acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert response["acquisition"]["status"] == "downloading"
    assert response["acquisition"]["queue_status"] == "downloading"
    assert response["acquisition"]["staged_sha256"] is None
    assert staged.is_file()


def test_verified_acquisition_hands_off_to_guarded_admission(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    calls = []
    monkeypatch.setattr(
        acquisition,
        "admit_staged_ebook",
        lambda result, relative_path, client: calls.append(
            (result["id"], relative_path)
        ) or {"admissionId": 91, "status": "scan_requested"},
    )

    response = acquisition.admit_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert response["acquisition"]["status"] == "admitted"
    assert response["acquisition"]["admission_id"] == 91
    assert calls == [(17, staged.name)]
    assert ebook_acquisition_by_id(started["acquisition"]["id"])["admission_id"] == 91
    assert staged.is_file()


def test_changed_staged_bytes_are_blocked_before_admission(acquisition_setup):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    staged.write_bytes(staged.read_bytes() + b"changed")

    with pytest.raises(acquisition.AcquisitionSafetyError, match="changed"):
        acquisition.admit_ebook_acquisition(
            started["acquisition"]["id"],
            setup["client"],
        )

    record = ebook_acquisition_by_id(started["acquisition"]["id"])
    assert record["status"] == "verified"
    assert record["admission_id"] is None


def test_finalize_registered_acquisition_removes_only_queue_and_staging(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "importExternal"}],
        "partial": False,
    }
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    acquisition.reconcile_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    monkeypatch.setattr(
        acquisition,
        "admit_staged_ebook",
        lambda result, relative_path, client: {
            "admissionId": 91,
            "status": "scan_requested",
        },
    )
    acquisition.admit_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )
    record = acquisition.ebook_acquisition_by_id(
        started["acquisition"]["id"]
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "result_id": 17,
            "book_id": 42,
            "status": "registered",
            "staged_relative_path": staged.name,
            "staged_sha256": record["staged_sha256"],
            "stored_path": setup["result"]["stored_path"],
            "local_path": setup["result"]["local_path"],
        },
    )
    Path(setup["result"]["local_path"]).write_bytes(staged.read_bytes())
    setup["client"].registered = True

    response = acquisition.finalize_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert response["acquisition"]["status"] == "finalized"
    assert response["queueRecordRemoved"] is True
    assert response["removedFromDownloadClient"] is False
    assert response["downloadedDataDeleted"] is False
    assert response["stagedFileRemoved"] is True
    assert not staged.exists()
    assert setup["client"].removed_queue_items == [(77, False, False)]


def test_finalize_refuses_unregistered_admission(acquisition_setup, monkeypatch):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    acquisition.update_ebook_acquisition(
        started["acquisition"]["id"],
        "admitted",
        admission_id=91,
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {"id": admission_id, "status": "scan_requested"},
    )

    with pytest.raises(acquisition.AcquisitionSafetyError, match="not registered"):
        acquisition.finalize_ebook_acquisition(
            started["acquisition"]["id"],
            setup["client"],
        )


def test_finalize_refuses_nonterminal_queue_and_retains_staging(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    _write_epub(staged, "Bel Canto", "Ann Patchett")
    staged_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
    acquisition.update_ebook_acquisition(
        started["acquisition"]["id"],
        "admitted",
        queue_id=77,
        queue_status="downloading",
        staged_relative_path=staged.name,
        staged_sha256=staged_hash,
        admission_id=91,
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "result_id": 17,
            "book_id": 42,
            "status": "registered",
            "staged_relative_path": staged.name,
            "staged_sha256": staged_hash,
            "stored_path": setup["result"]["stored_path"],
            "local_path": setup["result"]["local_path"],
        },
    )
    Path(setup["result"]["local_path"]).write_bytes(staged.read_bytes())
    setup["client"].registered = True
    setup["client"].queue = {
        "items": [{"id": 77, "bookId": 42, "status": "downloading"}],
        "partial": False,
    }

    with pytest.raises(acquisition.AcquisitionSafetyError, match="not safe"):
        acquisition.finalize_ebook_acquisition(
            started["acquisition"]["id"],
            setup["client"],
        )

    assert staged.is_file()
    assert setup["client"].removed_queue_items == []


def test_interrupted_finalize_refuses_symlinked_staging_path(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    outside = setup["staging"].parent / "outside.epub"
    outside.write_bytes(b"must remain")
    staged = setup["staging"] / "Bel Canto - Ann Patchett.epub"
    staged.symlink_to(outside)
    staged_hash = hashlib.sha256(outside.read_bytes()).hexdigest()
    acquisition.update_ebook_acquisition(
        started["acquisition"]["id"],
        "cleanup_required",
        queue_id=77,
        queue_status="removed",
        staged_relative_path=staged.name,
        staged_sha256=staged_hash,
        admission_id=91,
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "result_id": 17,
            "book_id": 42,
            "status": "registered",
            "staged_relative_path": staged.name,
            "staged_sha256": staged_hash,
            "stored_path": setup["result"]["stored_path"],
            "local_path": setup["result"]["local_path"],
        },
    )
    Path(setup["result"]["local_path"]).write_bytes(outside.read_bytes())
    setup["client"].registered = True

    with pytest.raises(acquisition.AcquisitionSafetyError, match="Symlinked"):
        acquisition.finalize_ebook_acquisition(
            started["acquisition"]["id"],
            setup["client"],
        )

    assert staged.is_symlink()
    assert outside.read_bytes() == b"must remain"


def test_finalize_reconciles_already_cleaned_registered_acquisition(
    acquisition_setup,
    monkeypatch,
):
    setup = acquisition_setup
    started = acquisition.start_ebook_acquisition(
        setup["result"],
        "safe-guid",
        setup["client"],
    )
    staged_name = "Bel Canto - Ann Patchett.epub"
    library = Path(setup["result"]["local_path"])
    _write_epub(library, "Bel Canto", "Ann Patchett")
    staged_hash = hashlib.sha256(library.read_bytes()).hexdigest()
    acquisition.update_ebook_acquisition(
        started["acquisition"]["id"],
        "admitted",
        queue_id=77,
        queue_status="downloading",
        staged_relative_path=staged_name,
        staged_sha256=staged_hash,
        admission_id=91,
    )
    monkeypatch.setattr(
        acquisition,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "result_id": 17,
            "book_id": 42,
            "status": "registered",
            "staged_relative_path": staged_name,
            "staged_sha256": staged_hash,
            "stored_path": setup["result"]["stored_path"],
            "local_path": setup["result"]["local_path"],
        },
    )
    setup["client"].registered = True

    response = acquisition.finalize_ebook_acquisition(
        started["acquisition"]["id"],
        setup["client"],
    )

    assert response["acquisition"]["status"] == "finalized"
    assert response["cleanupAlreadyComplete"] is True
    assert response["removedFromDownloadClient"] is False
    assert response["downloadedDataDeleted"] is False
    assert library.is_file()
    assert setup["client"].removed_queue_items == []
