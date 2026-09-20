from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import threading
from typing import Any

from .acquisition import (
    AcquisitionSafetyError,
    finalize_ebook_acquisition,
    reconcile_ebook_acquisition,
)
from .admission import AdmissionSafetyError, reconcile_admission
from .bindery_client import BinderyClient
from .config import ConfigurationError, load_automation_settings, settings
from .db import (
    active_ebook_acquisitions,
    ebook_admission_by_id,
)


_RECONCILE_STATUSES = {
    "preparing",
    "grab_requested",
    "queued",
    "downloading",
    "awaiting_staging",
    "staging_observed",
}
_FINALIZE_STATUSES = {
    "finalizing",
    "cleanup_required",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _acquisition_summary(acquisition: dict[str, Any] | None) -> dict[str, Any] | None:
    if not acquisition:
        return None
    return {
        "id": acquisition.get("id"),
        "bookId": acquisition.get("book_id"),
        "status": acquisition.get("status"),
        "admissionId": acquisition.get("admission_id"),
    }


class SupervisedAcquisitionCoordinator:
    """Advance one operator-started acquisition without choosing or admitting it.

    The coordinator never searches for a release, starts a grab, or approves
    admission. It resumes durable acquisition state only while all explicit
    environment and action gates remain enabled.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "state": "disabled",
            "action": None,
            "acquisition": None,
            "blockers": ["coordinatorEnabled"],
            "lastRunAt": None,
            "lastSuccessAt": None,
            "lastError": None,
        }

    def _replace_status(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            self._status.update(values)
            return deepcopy(self._status)

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = deepcopy(self._status)
            snapshot["running"] = bool(
                self._thread and self._thread.is_alive()
            )
            return snapshot

    def start(self) -> bool:
        try:
            configured = load_automation_settings()
        except ConfigurationError as exc:
            self._replace_status(
                enabled=False,
                running=False,
                state="error",
                blockers=["validConfiguration"],
                lastError=str(exc),
            )
            return False

        if not configured.acquisition_coordinator_enabled:
            self._replace_status(
                enabled=False,
                running=False,
                state="disabled",
                action=None,
                acquisition=None,
                blockers=["coordinatorEnabled"],
                lastError=None,
            )
            return False

        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="bookguard-acquisition-coordinator",
                daemon=True,
            )
            self._status.update({
                "enabled": True,
                "running": True,
                "state": "starting",
                "blockers": [],
                "lastError": None,
            })
            self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._replace_status(running=False)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            try:
                interval = load_automation_settings().acquisition_coordinator_interval_seconds
            except ConfigurationError as exc:
                self._replace_status(
                    state="error",
                    blockers=["validConfiguration"],
                    lastError=str(exc),
                )
                interval = 30
            self._stop.wait(interval)

    def run_once(
        self,
        client: BinderyClient | None = None,
    ) -> dict[str, Any]:
        """Advance at most one safe state transition and return worker status."""
        now = _utc_now()
        try:
            configured = load_automation_settings()
        except ConfigurationError as exc:
            return self._replace_status(
                enabled=False,
                state="error",
                action=None,
                blockers=["validConfiguration"],
                lastRunAt=now,
                lastError=str(exc),
            )

        if not configured.acquisition_coordinator_enabled:
            return self._replace_status(
                enabled=False,
                state="disabled",
                action=None,
                acquisition=None,
                blockers=["coordinatorEnabled"],
                lastRunAt=now,
                lastError=None,
            )

        blockers = []
        if not configured.automatic_reacquisition:
            blockers.append("automaticReacquisitionEnabled")
        if not settings.allow_actions:
            blockers.append("actionsEnabled")
        if blockers:
            return self._replace_status(
                enabled=True,
                state="blocked",
                action=None,
                blockers=blockers,
                lastRunAt=now,
                lastError=None,
            )

        acquisitions = active_ebook_acquisitions()
        if not acquisitions:
            return self._replace_status(
                enabled=True,
                state="idle",
                action=None,
                acquisition=None,
                blockers=[],
                lastRunAt=now,
                lastSuccessAt=now,
                lastError=None,
            )
        if len(acquisitions) != 1:
            return self._replace_status(
                enabled=True,
                state="blocked",
                action=None,
                acquisition=None,
                blockers=["singleActiveAcquisition"],
                lastRunAt=now,
                lastError=(
                    "The supervised coordinator found multiple active acquisitions."
                ),
            )

        acquisition = acquisitions[0]
        summary = _acquisition_summary(acquisition)
        status = str(acquisition.get("status") or "")
        client = client or BinderyClient()

        try:
            if status in _RECONCILE_STATUSES:
                response = reconcile_ebook_acquisition(
                    int(acquisition["id"]),
                    client,
                )
                updated = response.get("acquisition") or acquisition
                updated_status = str(updated.get("status") or "")
                state = (
                    "awaiting_admission"
                    if updated_status == "verified"
                    else "attention_required"
                    if updated_status in {"failed", "review_required"}
                    else "monitoring"
                )
                return self._replace_status(
                    enabled=True,
                    state=state,
                    action="reconcile_acquisition",
                    acquisition=_acquisition_summary(updated),
                    blockers=[],
                    lastRunAt=now,
                    lastSuccessAt=now,
                    lastError=None,
                )

            if status == "verified":
                return self._replace_status(
                    enabled=True,
                    state="awaiting_admission",
                    action=None,
                    acquisition=summary,
                    blockers=["explicitAdmissionRequired"],
                    lastRunAt=now,
                    lastSuccessAt=now,
                    lastError=None,
                )

            if status == "admitted":
                admission_id = acquisition.get("admission_id")
                admission = (
                    ebook_admission_by_id(int(admission_id))
                    if admission_id is not None
                    else None
                )
                if not admission:
                    raise AcquisitionSafetyError(
                        "The admitted acquisition has no linked admission record."
                    )
                admission_status = str(admission.get("status") or "")
                if admission_status in {
                    "registration_conflict",
                    "registration_correcting",
                }:
                    correction_interrupted = (
                        admission_status == "registration_correcting"
                    )
                    return self._replace_status(
                        enabled=True,
                        state="attention_required",
                        action=None,
                        acquisition=summary,
                        blockers=[
                            "registrationCorrectionInterrupted"
                            if correction_interrupted
                            else "registrationConflict"
                        ],
                        lastRunAt=now,
                        lastError=(
                            str(admission.get("error") or "")
                            or (
                                "An explicit Bindery registration correction must be "
                                "resumed."
                                if correction_interrupted
                                else "Bindery assigned the admitted path to a different book."
                            )
                        ),
                    )
                if admission_status != "registered":
                    response = reconcile_admission(int(admission_id), client)
                    if not response.get("registered"):
                        if response.get("status") == "registration_conflict":
                            return self._replace_status(
                                enabled=True,
                                state="attention_required",
                                action="reconcile_admission",
                                acquisition=summary,
                                blockers=["registrationConflict"],
                                lastRunAt=now,
                                lastError=str(response.get("message") or ""),
                            )
                        return self._replace_status(
                            enabled=True,
                            state="awaiting_registration",
                            action="reconcile_admission",
                            acquisition=summary,
                            blockers=[],
                            lastRunAt=now,
                            lastSuccessAt=now,
                            lastError=None,
                        )
                response = finalize_ebook_acquisition(
                    int(acquisition["id"]),
                    client,
                )
                return self._replace_status(
                    enabled=True,
                    state="idle",
                    action="finalize_acquisition",
                    acquisition=_acquisition_summary(response.get("acquisition")),
                    blockers=[],
                    lastRunAt=now,
                    lastSuccessAt=now,
                    lastError=None,
                )

            if status in _FINALIZE_STATUSES:
                response = finalize_ebook_acquisition(
                    int(acquisition["id"]),
                    client,
                )
                return self._replace_status(
                    enabled=True,
                    state="idle",
                    action="finalize_acquisition",
                    acquisition=_acquisition_summary(response.get("acquisition")),
                    blockers=[],
                    lastRunAt=now,
                    lastSuccessAt=now,
                    lastError=None,
                )

            raise AcquisitionSafetyError(
                f"Acquisition status '{status}' is not coordinator-managed."
            )
        except (AcquisitionSafetyError, AdmissionSafetyError) as exc:
            return self._replace_status(
                enabled=True,
                state="blocked",
                action=None,
                acquisition=summary,
                blockers=["safeTransitionAvailable"],
                lastRunAt=now,
                lastError=str(exc),
            )
        except Exception as exc:
            return self._replace_status(
                enabled=True,
                state="error",
                action=None,
                acquisition=summary,
                blockers=["coordinatorHealthy"],
                lastRunAt=now,
                lastError=str(exc),
            )


coordinator = SupervisedAcquisitionCoordinator()


def start_acquisition_coordinator() -> bool:
    return coordinator.start()


def stop_acquisition_coordinator() -> None:
    coordinator.stop()


def acquisition_coordinator_status() -> dict[str, Any]:
    return coordinator.status()
