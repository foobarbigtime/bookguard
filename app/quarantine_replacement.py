"""Read-only custody proof before any future unsafe-media replacement grab."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import automatic_execution as core
from .acquisition import AcquisitionSafetyError, _result_and_book
from .bindery_client import BinderyClient
from .config import settings
from .db import associations_inside_path, local_conn, result_by_id
from .file_safety import sha256_file
from .observe import _result_decisions
from .recovery_planner import _build_plan, recovery_plan_by_id


def _current_plan(result_id: int) -> dict[str, Any] | None:
    with local_conn() as conn:
        decision = next((
            item for item in _result_decisions(conn, 500)
            if item.get("subjectKind") == "result"
            and str(item.get("subjectId")) == str(result_id)
        ), None)
        return _build_plan(conn, decision) if decision else None


def _prior_acquisition(result_id: int) -> bool:
    with local_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM ebook_acquisitions WHERE result_id=? LIMIT 1",
            (result_id,),
        ).fetchone() is not None


def quarantine_replacement_preview(
    plan_id: int, client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Prove retained unsafe bytes and detached ownership without choosing a release."""
    plan = recovery_plan_by_id(int(plan_id))
    if not plan:
        raise ValueError("Recovery plan not found.")

    steps = list(plan.get("steps") or [])
    index = int(plan.get("currentStep") or 0)
    result_id = int(plan.get("subjectId") or 0) if plan.get("subjectKind") == "result" else 0
    result = result_by_id(result_id) if result_id else None
    receipt = core._existing(str(plan.get("signature") or ""), "quarantine_exact_media", 2)
    outcome = dict((receipt or {}).get("externalResult") or {})
    boundary = dict((receipt or {}).get("boundary") or {})
    current = _current_plan(result_id) if result else None

    expected_root = Path(settings.quarantine_root).absolute()
    expected_dir = expected_root / str(plan.get("bookId") or "")
    raw_path = str(outcome.get("quarantinePath") or "")
    path = Path(raw_path) if raw_path else None
    safe_path = bool(
        path and path.is_absolute() and path.parent == expected_dir
        and not expected_root.is_symlink() and not expected_dir.is_symlink()
        and not path.is_symlink()
    )
    sha = str(outcome.get("sha256") or "")
    try:
        bytes_match = bool(safe_path and path.is_file() and len(sha) == 64
                           and sha256_file(path) == sha)
    except OSError:
        bytes_match = False

    source = Path(str((result or {}).get("local_path") or "")) if result else None
    source_absent = bool(source and source.is_absolute()
                         and not source.exists() and not source.is_symlink())
    try:
        detached = bool(result and not associations_inside_path(
            str(result.get("stored_path") or "")))
    except (OSError, ValueError):
        detached = False
    try:
        if result:
            _result_and_book(result, client or BinderyClient())
        book_ready = bool(result)
    except AcquisitionSafetyError:
        book_ready = False

    checks = [
        {"code": "CURRENT_QUARANTINE_STEP", "ok": bool(
            plan.get("planKind") == "QUARANTINE_UNSAFE_MEDIA"
            and plan.get("subjectKind") == "result"
            and plan.get("state") == "ready" and index == 3
            and len(steps) > 3 and steps[2].get("code") == "quarantine_exact_media"
            and steps[3].get("code") == "reacquire_expected_media"
        )},
        {"code": "CURRENT_DECISION_IDENTITY", "ok": bool(
            current and current.get("signature") == plan.get("signature")
            and current.get("evidenceRevision") == plan.get("evidenceRevision")
        )},
        {"code": "PROVEN_QUARANTINE_RECEIPT", "ok": bool(
            receipt and receipt.get("state") == "succeeded"
            and receipt.get("planId") == plan.get("id")
            and receipt.get("attemptCount") == 1
            and outcome.get("status") == "quarantined"
            and outcome.get("binderyDetached") is True
            and outcome.get("permanentDeletion") is False
            and outcome.get("replacementRequested") is False
            and result and outcome.get("resultId") == result_id
            and outcome.get("fileId") == int(result["file_id"])
            and outcome.get("bookId") == int(result["book_id"])
            and boundary.get("expectedSha256") == sha
            and boundary.get("storedPath") == result.get("stored_path")
            and boundary.get("localPath") == result.get("local_path")
        )},
        {"code": "QUARANTINE_BYTES_MATCH_RECEIPT", "ok": bytes_match},
        {"code": "ORIGINAL_SOURCE_ABSENT", "ok": source_absent},
        {"code": "EXACT_BINDERY_ASSOCIATION_ABSENT", "ok": detached},
        {"code": "CURRENT_BOOK_UNOCCUPIED", "ok": book_ready},
        {"code": "NO_PRIOR_ACQUISITION", "ok": bool(
            result and not _prior_acquisition(result_id)
        )},
    ]
    return {
        "planId": int(plan["id"]),
        "resultId": result_id or None,
        "safeForCandidateReview": all(check["ok"] for check in checks),
        "liveGrabEnabled": False,
        "checks": checks,
        "message": "Quarantine custody preview only; no release was selected or grabbed.",
    }
