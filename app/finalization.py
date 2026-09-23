from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .acquisition import (
    AcquisitionSafetyError,
    _FINALIZABLE_QUEUE_STATUSES,
    _FINALIZABLE_STATUSES,
    _automation_settings,
    _queue_payload,
    _queue_status,
)
from .bindery_client import BinderyClient, BinderyClientError
from .config import settings
from .db import ebook_acquisition_by_id, ebook_admission_by_id, result_by_id
from .file_safety import sha256_file
from .staging import StagingSafetyError, resolve_staged_file, staged_path_is_absent


def _queue_identity_matches(acquisition: dict[str, Any], item: dict[str, Any]) -> bool:
    if str(item.get("bookId") or "") != str(acquisition.get("book_id") or ""):
        return False

    expected_title = str(acquisition.get("candidate_title") or "").strip().casefold()
    if expected_title and str(item.get("title") or "").strip().casefold() != expected_title:
        return False

    expected_protocol = str(acquisition.get("candidate_protocol") or "").strip().casefold()
    if (
        expected_protocol
        and str(item.get("protocol") or "").strip().casefold() != expected_protocol
    ):
        return False
    return True


def finalization_preview(
    acquisition_id: int,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Prove the exact current finalization boundary without mutating anything.

    The existing ``finalize_ebook_acquisition`` transaction remains the only cleanup
    primitive. This preview is deliberately stricter: Automatic Mode must prove the
    durable acquisition/admission identity, published bytes, Bindery registration,
    exact queue identity, and current staging state before that primitive may run.
    """
    client = client or BinderyClient()
    acquisition = ebook_acquisition_by_id(int(acquisition_id))
    if not acquisition:
        raise AcquisitionSafetyError("Acquisition record not found.")

    status = str(acquisition.get("status") or "").casefold()
    configured = _automation_settings()
    admission_id = acquisition.get("admission_id")
    admission = ebook_admission_by_id(int(admission_id)) if admission_id else None
    expected_hash = str(acquisition.get("staged_sha256") or "")
    relative_path = str(acquisition.get("staged_relative_path") or "")

    linked_admission_registered = bool(
        admission and str(admission.get("status") or "").casefold() == "registered"
    )
    acquisition_admission_identity = bool(
        admission
        and expected_hash
        and relative_path
        and expected_hash == str(admission.get("staged_sha256") or "")
        and relative_path == str(admission.get("staged_relative_path") or "")
        and int(admission.get("book_id") or 0) == int(acquisition.get("book_id") or 0)
        and int(admission.get("result_id") or 0) == int(acquisition.get("result_id") or 0)
    )

    result = result_by_id(int(acquisition.get("result_id") or 0))
    result_identity = bool(
        result
        and admission
        and str(result.get("stored_path") or "")
        == str(admission.get("stored_path") or "")
        and str(result.get("local_path") or "")
        == str(admission.get("local_path") or "")
    )

    library_path = Path(str((admission or {}).get("local_path") or ""))
    library_bytes_current = False
    if expected_hash and library_path.is_file() and not library_path.is_symlink():
        library_bytes_current = sha256_file(library_path) == expected_hash

    bindery_registration_current = False
    try:
        book = client.get_book(int(acquisition.get("book_id") or 0))
    except BinderyClientError as exc:
        raise AcquisitionSafetyError(str(exc)) from exc
    if admission:
        registered_path = os.path.normpath(str(admission.get("stored_path") or ""))
        bindery_registration_current = any(
            isinstance(item, dict)
            and str(item.get("format") or "").casefold() == "ebook"
            and os.path.normpath(str(item.get("path") or "")) == registered_path
            for item in (book.get("bookFiles") or [])
        )

    queue_items, queue_partial = _queue_payload(client)
    queue_id = acquisition.get("queue_id")
    queue_matches = (
        [
            item
            for item in queue_items
            if str(item.get("id") or "") == str(queue_id)
        ]
        if queue_id is not None
        else []
    )
    queue_item = queue_matches[0] if len(queue_matches) == 1 else None
    queue_present = queue_item is not None
    queue_status = _queue_status(queue_item) if queue_item else "absent"
    queue_status_finalizable = bool(
        not queue_item or queue_status in _FINALIZABLE_QUEUE_STATUSES
    )
    queue_identity_current = bool(
        not queue_item or _queue_identity_matches(acquisition, queue_item)
    )

    staged_present = False
    staged_bytes_current = False
    staged_safely_absent = False
    try:
        _, staged_path = resolve_staged_file(relative_path)
        staged_present = True
        staged_bytes_current = bool(
            expected_hash and sha256_file(staged_path) == expected_hash
        )
    except StagingSafetyError:
        if (
            not queue_present
            and status in {"admitted", "finalizing", "cleanup_required"}
        ):
            try:
                staged_safely_absent = staged_path_is_absent(relative_path)
            except StagingSafetyError:
                staged_safely_absent = False

    if queue_present and staged_present:
        cleanup_state = "queue_and_staging_pending"
    elif not queue_present and staged_present:
        cleanup_state = "staging_pending"
    elif not queue_present and staged_safely_absent:
        cleanup_state = "complete"
    else:
        cleanup_state = "inconsistent"

    staging_state_safe = bool(
        (staged_present and staged_bytes_current)
        or (not staged_present and staged_safely_absent and not queue_present)
    )

    checks = {
        "actionsEnabled": bool(settings.allow_actions),
        "automaticReacquisitionEnabled": bool(configured.automatic_reacquisition),
        "workflowStateFinalizable": status in _FINALIZABLE_STATUSES,
        "linkedAdmissionRegistered": linked_admission_registered,
        "acquisitionAdmissionIdentityConsistent": acquisition_admission_identity,
        "resultIdentityConsistent": result_identity,
        "libraryBytesCurrent": library_bytes_current,
        "binderyRegistrationCurrent": bindery_registration_current,
        "queueResponseComplete": not queue_partial,
        "queueIdPresent": queue_id is not None,
        "queueIdentityUnambiguous": len(queue_matches) <= 1,
        "queueIdentityCurrent": queue_identity_current,
        "queueStatusFinalizable": queue_status_finalizable,
        "stagingStateSafe": staging_state_safe,
        "cleanupStateConsistent": cleanup_state != "inconsistent",
    }
    safe = all(checks.values())
    return {
        "safe": safe,
        "checks": checks,
        "acquisitionId": int(acquisition["id"]),
        "admissionId": int(admission["id"]) if admission else None,
        "resultId": int(acquisition["result_id"]),
        "bookId": int(acquisition["book_id"]),
        "status": status,
        "storedPath": str((admission or {}).get("stored_path") or ""),
        "localPath": str((admission or {}).get("local_path") or ""),
        "stagedRelativePath": relative_path,
        "stagedSha256": expected_hash,
        "queueId": queue_id,
        "queuePresent": queue_present,
        "queueStatus": queue_status,
        "stagedPresent": staged_present,
        "stagedSafelyAbsent": staged_safely_absent,
        "cleanupState": cleanup_state,
        "message": (
            "Guarded finalization is freshly proven safe."
            if safe
            else "Guarded finalization is blocked until every current invariant is proven."
        ),
    }
