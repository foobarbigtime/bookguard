"""Coordinate the read-only Check library workflow and serialize job starts.

The scanner and verifier still own their work. This coordinator reserves their
sequence so a second scan cannot replace the results during verification.
Status is process-local, just like the underlying jobs; database results survive
a restart, but an interrupted workflow is not automatically resumed.
"""
from __future__ import annotations

from functools import wraps
import threading
import time
import uuid

from .db import latest_scan

_lock = threading.RLock()
_state: dict = {"status": "idle", "phase": "idle", "check_id": None, "error": None}


def serialized_start(function):
    @wraps(function)
    def start(*args, **kwargs):
        with _lock:
            return function(*args, **kwargs)
    return start


def start_allowed(check_id: str | None = None) -> bool:
    with _lock:
        return _state["status"] != "running" or check_id == _state["check_id"]


def _update(**values) -> None:
    with _lock:
        _state.update(values)


def library_check_status() -> dict:
    from .scanner import current_scan_detail, scan_is_running
    from .duplicates import fix_status
    from .unmatched import check_status
    from .verifier import verification_job_status

    with _lock:
        state = dict(_state)
    state["scan"] = latest_scan()
    state["scan_detail"] = current_scan_detail()
    state["verification"] = verification_job_status()
    state["unmatched"] = check_status()
    if state["status"] != "running" and state.get("scan_id") and (state["scan"] or {}).get("id") != state["scan_id"]:
        state.update(status="idle", phase="idle", error=None)
    state["busy"] = (
        state["status"] == "running" or scan_is_running()
        or state["verification"]["status"] == "running"
        or state["unmatched"]["running"]
        or fix_status()["running"]
    )
    return state


@serialized_start
def start_library_check() -> str | None:
    from .scanner import scan_is_running, start_scan
    from .duplicates import fix_status
    from .unmatched import check_status, start_check
    from .verifier import start_verification_job, verification_job_status

    if (_state["status"] == "running" or scan_is_running()
            or verification_job_status()["status"] == "running"
            or check_status()["running"] or fix_status()["running"]):
        return None
    check_id = uuid.uuid4().hex
    _state.clear()
    _state.update(status="running", phase="starting", check_id=check_id,
                  scan_id=None, job_id=None, error=None)

    def worker() -> None:
        try:
            scan_id = start_scan(check_id=check_id)
            if not scan_id:
                raise RuntimeError("Another scan started before this check.")
            _update(phase="scan", scan_id=scan_id)
            while scan_is_running():
                time.sleep(0.25)
            scan = latest_scan()
            if not scan or scan["id"] != scan_id or scan["status"] != "complete":
                raise RuntimeError("The scan did not complete. Verification was not started.")
            _update(phase="verification")
            job_id = start_verification_job("ALL", check_id=check_id, expected_scan_id=scan_id)
            if not job_id:
                raise RuntimeError("Another verification job started before this check.")
            _update(job_id=job_id)
            while verification_job_status()["status"] == "running":
                time.sleep(0.25)
            job = verification_job_status()
            if job.get("job_id") != job_id or job["status"] != "complete":
                raise RuntimeError(job.get("error") or "Verification did not complete.")
            _update(phase="unmatched")
            if not start_check(check_id=check_id):
                raise RuntimeError("Another unassigned-file check started before this check.")
            while check_status()["running"]:
                time.sleep(0.25)
            if check_status().get("error"):
                raise RuntimeError("Unassigned files: " + check_status()["error"])
            _update(status="complete", phase="complete", verification_result=job,
                    unmatched_result=check_status())
        except Exception as exc:
            _update(status="failed", error=str(exc)[:1000])

    threading.Thread(target=worker, name=f"bookguard-check-{check_id[:8]}", daemon=True).start()
    return check_id
