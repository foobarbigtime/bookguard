import threading
from types import SimpleNamespace

from app import acquisition_coordinator as coordinator_module


def _configured(*, enabled=True, automatic=True):
    return SimpleNamespace(
        acquisition_coordinator_enabled=enabled,
        acquisition_coordinator_interval_seconds=10,
        automatic_reacquisition=automatic,
    )


def _acquisition(status: str, *, admission_id=None):
    return {
        "id": 12,
        "book_id": 42,
        "status": status,
        "admission_id": admission_id,
    }


def test_disabled_coordinator_does_not_read_active_state(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(enabled=False),
    )
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: (_ for _ in ()).throw(AssertionError("active state was read")),
    )

    status = worker.run_once(object())

    assert status["state"] == "disabled"
    assert status["blockers"] == ["coordinatorEnabled"]


def test_coordinator_requires_both_mutation_gates(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(automatic=False),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", False)

    status = worker.run_once(object())

    assert status["state"] == "blocked"
    assert status["blockers"] == [
        "automaticReacquisitionEnabled",
        "actionsEnabled",
    ]


def test_coordinator_resumes_and_reconciles_durable_download(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", True)
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: [_acquisition("downloading")],
    )
    calls = []
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_ebook_acquisition",
        lambda acquisition_id, client: calls.append(acquisition_id)
        or {"acquisition": _acquisition("awaiting_staging")},
    )

    status = worker.run_once(object())

    assert calls == [12]
    assert status["state"] == "monitoring"
    assert status["action"] == "reconcile_acquisition"
    assert status["acquisition"]["status"] == "awaiting_staging"


def test_coordinator_pauses_at_verified_for_explicit_admission(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", True)
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: [_acquisition("verified")],
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_admission",
        lambda *args: (_ for _ in ()).throw(AssertionError("admission was approved")),
    )

    status = worker.run_once(object())

    assert status["state"] == "awaiting_admission"
    assert status["action"] is None
    assert status["blockers"] == ["explicitAdmissionRequired"]


def test_coordinator_finishes_after_operator_admission(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", True)
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: [_acquisition("admitted", admission_id=91)],
    )
    monkeypatch.setattr(
        coordinator_module,
        "ebook_admission_by_id",
        lambda admission_id: {"id": admission_id, "status": "scan_requested"},
    )
    calls = []
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_admission",
        lambda admission_id, client: calls.append(("register", admission_id))
        or {"registered": True},
    )
    monkeypatch.setattr(
        coordinator_module,
        "finalize_ebook_acquisition",
        lambda acquisition_id, client: calls.append(("finalize", acquisition_id))
        or {"acquisition": _acquisition("finalized", admission_id=91)},
    )

    status = worker.run_once(object())

    assert calls == [("register", 91), ("finalize", 12)]
    assert status["state"] == "idle"
    assert status["action"] == "finalize_acquisition"
    assert status["acquisition"]["status"] == "finalized"


def test_coordinator_waits_when_registration_is_not_complete(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", True)
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: [_acquisition("admitted", admission_id=91)],
    )
    monkeypatch.setattr(
        coordinator_module,
        "ebook_admission_by_id",
        lambda admission_id: {"id": admission_id, "status": "scan_requested"},
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_admission",
        lambda admission_id, client: {"registered": False},
    )
    monkeypatch.setattr(
        coordinator_module,
        "finalize_ebook_acquisition",
        lambda *args: (_ for _ in ()).throw(AssertionError("finalized too soon")),
    )

    status = worker.run_once(object())

    assert status["state"] == "awaiting_registration"
    assert status["action"] == "reconcile_admission"


def test_coordinator_stops_on_existing_registration_conflict(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", True)
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: [_acquisition("admitted", admission_id=91)],
    )
    monkeypatch.setattr(
        coordinator_module,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "status": "registration_conflict",
            "error": "Bindery assigned the path to book #77.",
        },
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_admission",
        lambda *args: (_ for _ in ()).throw(AssertionError("scan loop resumed")),
    )

    status = worker.run_once(object())

    assert status["state"] == "attention_required"
    assert status["action"] is None
    assert status["blockers"] == ["registrationConflict"]
    assert "book #77" in status["lastError"]


def test_coordinator_never_resumes_explicit_registration_correction(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", True)
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: [_acquisition("admitted", admission_id=91)],
    )
    monkeypatch.setattr(
        coordinator_module,
        "ebook_admission_by_id",
        lambda admission_id: {
            "id": admission_id,
            "status": "registration_correcting",
            "error": "Explicit recovery is required.",
        },
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_admission",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("explicit correction resumed automatically")
        ),
    )

    status = worker.run_once(object())

    assert status["state"] == "attention_required"
    assert status["action"] is None
    assert status["blockers"] == ["registrationCorrectionInterrupted"]
    assert "Explicit recovery" in status["lastError"]


def test_coordinator_surfaces_new_registration_conflict(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", True)
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: [_acquisition("admitted", admission_id=91)],
    )
    monkeypatch.setattr(
        coordinator_module,
        "ebook_admission_by_id",
        lambda admission_id: {"id": admission_id, "status": "scan_requested"},
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_admission",
        lambda admission_id, client: {
            "registered": False,
            "status": "registration_conflict",
            "message": "Bindery assigned the path to book #77.",
        },
    )

    status = worker.run_once(object())

    assert status["state"] == "attention_required"
    assert status["action"] == "reconcile_admission"
    assert status["blockers"] == ["registrationConflict"]
    assert "book #77" in status["lastError"]


def test_coordinator_fails_closed_for_multiple_active_acquisitions(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    monkeypatch.setattr(coordinator_module.settings, "allow_actions", True)
    monkeypatch.setattr(
        coordinator_module,
        "active_ebook_acquisitions",
        lambda: [_acquisition("queued"), _acquisition("downloading")],
    )

    status = worker.run_once(object())

    assert status["state"] == "blocked"
    assert status["blockers"] == ["singleActiveAcquisition"]
    assert "multiple active" in status["lastError"]


def test_enabled_coordinator_starts_and_stops_one_background_thread(monkeypatch):
    worker = coordinator_module.SupervisedAcquisitionCoordinator()
    monkeypatch.setattr(
        coordinator_module,
        "load_automation_settings",
        lambda: _configured(),
    )
    called = threading.Event()
    monkeypatch.setattr(worker, "run_once", lambda: called.set())

    assert worker.start() is True
    assert called.wait(1.0) is True
    assert worker.status()["running"] is True

    worker.stop()

    assert worker.status()["running"] is False
