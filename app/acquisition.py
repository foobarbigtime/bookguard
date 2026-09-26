from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import Any, Callable

from .admission import AdmissionSafetyError, admit_staged_ebook
from .bindery_client import (
    BinderyClient,
    BinderyClientError,
    evaluate_replacement_candidate,
)
from .config import ConfigurationError, load_automation_settings, settings
from .db import (
    active_ebook_acquisitions,
    create_ebook_acquisition,
    ebook_admission_by_id,
    ebook_acquisition_by_id,
    recent_ebook_acquisitions,
    reset_ebook_acquisition_for_retry,
    result_by_id,
    update_ebook_acquisition,
)
from .file_safety import sha256_file
from .preimport import PreImportSafetyError, preimport_readiness
from .scan_guard import (
    CurrentScanError,
    NO_COMPLETE_SCAN,
    require_current_scan_result as require_guarded_scan_result,
)
from .staging import (
    StagingSafetyError,
    book_identity,
    list_staged_ebooks,
    resolve_staged_file,
    staged_path_is_absent,
    verify_staged_ebook,
)


class AcquisitionSafetyError(RuntimeError):
    pass


_acquisition_lock = threading.Lock()
_HISTORY_PROOF_LIMIT = 200
_HISTORICAL_QUEUE_STATUSES = {
    "cancelled",
    "failed",
    "importblocked",
    "imported",
    "removed",
}
_FAILED_QUEUE_STATUSES = {"failed", "importblocked", "importfailed"}
_AWAITING_STAGING_QUEUE_STATUSES = {
    "completed",
    "importexternal",
    "importheld",
    "importpending",
    "importing",
}
_RECONCILABLE_STATUSES = {
    "preparing",
    "grab_requested",
    "queued",
    "downloading",
    "awaiting_staging",
    "staging_observed",
    "verified",
    # An operator may explicitly retry unchanged staged bytes after verifier
    # rules are improved. The coordinator intentionally does not treat this as
    # active, so review never resumes automatically.
    "review_required",
}
_FINALIZABLE_STATUSES = {
    "admitted",
    "finalizing",
    "cleanup_required",
    "finalized",
}
_FINALIZABLE_QUEUE_STATUSES = {
    "importexternal",
    "imported",
    "removed",
}


def _automation_settings():
    try:
        return load_automation_settings()
    except ConfigurationError as exc:
        raise AcquisitionSafetyError(str(exc)) from exc


def _queue_payload(
    client: BinderyClient,
) -> tuple[list[dict[str, Any]], bool]:
    try:
        payload = client.list_queue()
    except BinderyClientError as exc:
        raise AcquisitionSafetyError(str(exc)) from exc

    if isinstance(payload, list):
        if not all(isinstance(item, dict) for item in payload):
            raise AcquisitionSafetyError("Bindery queue contained an unrecognized record.")
        return payload, False
    if not isinstance(payload, dict):
        raise AcquisitionSafetyError("Bindery returned an invalid queue response.")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise AcquisitionSafetyError("Bindery queue response did not contain an item list.")
    items = [item for item in raw_items if isinstance(item, dict)]
    if len(items) != len(raw_items):
        raise AcquisitionSafetyError("Bindery queue contained an unrecognized record.")
    return items, bool(payload.get("partial", False))


def _queue_status(item: dict[str, Any]) -> str:
    return str(item.get("status") or "").strip().casefold()


def _active_or_unknown_queue_items(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if not _queue_status(item)
        or _queue_status(item) not in _HISTORICAL_QUEUE_STATUSES
    ]


def _queue_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "bookId": item.get("bookId"),
        "title": item.get("title"),
        "status": item.get("status"),
        "protocol": item.get("protocol"),
    }


def _acquisition_status_for_queue(queue_status: str) -> str:
    if queue_status in _AWAITING_STAGING_QUEUE_STATUSES:
        return "awaiting_staging"
    if queue_status == "downloading":
        return "downloading"
    return "queued"


def _bindery_setting_text(
    client: BinderyClient,
    key: str,
    default: str,
) -> str:
    try:
        value = client.get_setting(key)
    except BinderyClientError as exc:
        if "HTTP 404" in str(exc):
            return default
        raise AcquisitionSafetyError(str(exc)) from exc
    if isinstance(value, dict):
        value = value.get("value")
    return str(value if value is not None else default).strip()


def acquisition_readiness(
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Report whether one controlled queue-to-staging acquisition may start."""
    client = client or BinderyClient()
    try:
        preimport = preimport_readiness(client)
        staging = list_staged_ebooks(1000)
        queue_items, queue_partial = _queue_payload(client)
        auto_grab = _bindery_setting_text(
            client,
            "autoGrab.enabled",
            "true",
        ).casefold()
    except (PreImportSafetyError, StagingSafetyError) as exc:
        raise AcquisitionSafetyError(str(exc)) from exc

    queue_active = _active_or_unknown_queue_items(queue_items)
    active_sessions = active_ebook_acquisitions()
    checks = {
        **preimport["checks"],
        "actionsEnabled": settings.allow_actions,
        "binderyAutoGrabDisabled": auto_grab == "false",
        "binderyQueueComplete": not queue_partial,
        "binderyQueueIdle": not queue_active,
        "stagingInventoryComplete": not staging["truncated"],
        "stagingEmpty": not staging["items"],
        "noActiveAcquisition": not active_sessions,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    ready = not blockers
    return {
        "ready": ready,
        "checks": checks,
        "blockers": blockers,
        "activeQueueItems": [_queue_summary(item) for item in queue_active],
        "activeAcquisitions": [
            {
                "id": item["id"],
                "bookId": item["book_id"],
                "status": item["status"],
            }
            for item in active_sessions
        ],
        "stagingCount": staging["count"],
        "message": (
            "One explicitly selected ebook acquisition may be started."
            if ready
            else "Acquisition is blocked until every queue and staging safety check passes."
        ),
    }


def _result_and_book(
    result: dict[str, Any],
    client: BinderyClient,
) -> tuple[dict[str, Any], str, str]:
    try:
        require_guarded_scan_result(result)
    except CurrentScanError as exc:
        if exc.code == NO_COMPLETE_SCAN:
            raise AcquisitionSafetyError("A completed latest scan is required.") from exc
        raise AcquisitionSafetyError(
            "The acquisition result is not from the latest completed scan."
        ) from exc
    if str(result.get("format") or "").casefold() != "ebook":
        raise AcquisitionSafetyError("Automatic acquisition currently supports ebooks only.")

    local_path = Path(str(result.get("local_path") or ""))
    if local_path.exists() or local_path.is_symlink():
        raise AcquisitionSafetyError(
            "The former ebook path still exists; acquisition will not replace it."
        )

    try:
        book = client.get_book(int(result["book_id"]))
        expected_title, expected_author = book_identity(book)
    except (BinderyClientError, StagingSafetyError) as exc:
        raise AcquisitionSafetyError(str(exc)) from exc

    if str(book.get("ebookFilePath") or "").strip() or any(
        isinstance(item, dict)
        and str(item.get("format") or "").casefold() == "ebook"
        for item in (book.get("bookFiles") or [])
    ):
        raise AcquisitionSafetyError("Bindery already tracks an ebook for this book.")

    if (
        str(result.get("title") or "").strip().casefold()
        != expected_title.casefold()
        or str(result.get("author") or "").strip().casefold()
        != expected_author.casefold()
    ):
        raise AcquisitionSafetyError(
            "The current Bindery book identity no longer matches the scan result."
        )
    return book, expected_title, expected_author


def _search_candidate(
    client: BinderyClient,
    book_id: int,
    candidate_guid: str,
    expected_title: str,
    expected_author: str,
) -> dict[str, Any]:
    try:
        payload = client.search_book(book_id)
    except BinderyClientError as exc:
        raise AcquisitionSafetyError(str(exc)) from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise AcquisitionSafetyError("Bindery search returned an invalid result list.")
    if payload.get("partial") not in (None, False):
        raise AcquisitionSafetyError(
            "Bindery search is incomplete; candidate identity cannot be proven."
        )

    matches = [
        item
        for item in results
        if isinstance(item, dict)
        and str(item.get("guid") or "") == candidate_guid
    ]
    if len(matches) != 1:
        raise AcquisitionSafetyError(
            "The selected release is no longer uniquely present in a fresh Bindery search."
        )

    candidate = matches[0]
    if str(candidate.get("mediaType") or "").casefold() != "ebook":
        raise AcquisitionSafetyError("The selected release is not explicitly an ebook.")
    decision = evaluate_replacement_candidate(
        candidate,
        expected_title=expected_title,
        expected_author=expected_author,
    )
    if not decision.safe:
        raise AcquisitionSafetyError(
            f"The selected release failed BookGuard's safety gate: {decision.reason}."
        )
    _reject_previously_imported_candidate(client, book_id, candidate)
    return candidate


def _reject_previously_imported_candidate(
    client: BinderyClient,
    book_id: int,
    candidate: dict[str, Any],
) -> None:
    """Reject an exact release title already paired as grabbed and imported."""
    try:
        payload = client.list_history(book_id, limit=_HISTORY_PROOF_LIMIT)
    except BinderyClientError as exc:
        raise AcquisitionSafetyError(str(exc)) from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise AcquisitionSafetyError("Bindery history returned an invalid item list.")
    if payload.get("partial") not in (None, False) or len(items) >= _HISTORY_PROOF_LIMIT:
        raise AcquisitionSafetyError(
            "Bindery history is incomplete; the release's import provenance "
            "cannot be proven safe."
        )

    title = str(candidate.get("title") or "").strip().casefold()
    matching_events = {
        str(item.get("eventType") or "").strip().casefold()
        for item in items
        if isinstance(item, dict)
        and str(item.get("sourceTitle") or "").strip().casefold() == title
    }
    if {"grabbed", "bookimported"}.issubset(matching_events):
        raise AcquisitionSafetyError(
            "The selected release already appears as grabbed and imported in "
            "Bindery history."
        )


def _response_queue_id(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    candidates = [response]
    for key in ("item", "queueItem", "queue"):
        if isinstance(response.get(key), dict):
            candidates.append(response[key])
    for candidate in candidates:
        raw = candidate.get("queueId", candidate.get("id"))
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _safe_grab_response(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    source = response
    for key in ("item", "queueItem", "queue"):
        if isinstance(response.get(key), dict):
            source = response[key]
            break
    summary = _queue_summary(source)
    summary["id"] = _response_queue_id(response)
    return summary


def start_ebook_acquisition(
    result: dict[str, Any],
    candidate_guid: str,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Freshly validate and grab exactly one release into external staging."""
    supplied_guid = str(candidate_guid or "").strip()
    if not supplied_guid:
        raise AcquisitionSafetyError("A candidate GUID is required.")
    client = client or BinderyClient()

    with _acquisition_lock:
        readiness = acquisition_readiness(client)
        if not readiness["ready"]:
            raise AcquisitionSafetyError(
                "Acquisition readiness failed: " + ", ".join(readiness["blockers"])
            )
        _, expected_title, expected_author = _result_and_book(result, client)
        candidate = _search_candidate(
            client,
            int(result["book_id"]),
            supplied_guid,
            expected_title,
            expected_author,
        )

        # Search can take time. Repeat the complete queue/staging preflight at
        # the last possible point before recording and requesting the grab.
        final_readiness = acquisition_readiness(client)
        if not final_readiness["ready"]:
            raise AcquisitionSafetyError(
                "Acquisition readiness changed during candidate validation: "
                + ", ".join(final_readiness["blockers"])
            )

        acquisition_id = create_ebook_acquisition(result, candidate)
        update_ebook_acquisition(acquisition_id, "grab_requested")
        try:
            response = client.grab(int(result["book_id"]), candidate)
        except Exception as exc:
            update_ebook_acquisition(
                acquisition_id,
                "failed",
                error=f"Bindery grab failed: {exc}",
            )
            raise AcquisitionSafetyError(f"Bindery grab failed: {exc}") from exc

        queue_id = _response_queue_id(response)
        try:
            update_ebook_acquisition(
                acquisition_id,
                "queued",
                queue_id=queue_id,
                grab_response=_safe_grab_response(response),
            )
        except Exception as exc:
            raise AcquisitionSafetyError(
                "Bindery accepted the grab, but BookGuard could not persist the response. "
                f"Acquisition {acquisition_id} remains recoverable: {exc}"
            ) from exc

    record = ebook_acquisition_by_id(acquisition_id)
    return {
        "ok": True,
        "acquisition": record,
        "message": (
            "Bindery accepted the explicit release. Reconcile this acquisition until "
            "one staged ebook independently verifies."
        ),
    }


def retry_failed_ebook_acquisition(
    acquisition_id: int,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Retry the exact candidate for one durably failed transient acquisition.

    This is intentionally narrower than starting a new acquisition. The existing
    acquisition row, result identity, candidate GUID/title/protocol, queue/staging
    readiness, and Bindery search result are all freshly revalidated before the
    same guarded grab is attempted again.
    """
    client = client or BinderyClient()

    with _acquisition_lock:
        acquisition = ebook_acquisition_by_id(int(acquisition_id))
        if not acquisition:
            raise AcquisitionSafetyError("Acquisition record not found.")
        if str(acquisition.get("status") or "").casefold() != "failed":
            raise AcquisitionSafetyError(
                "Only a durably failed acquisition may be retried automatically."
            )

        from .recovery_classifier import classify_acquisition_failure

        recovery = classify_acquisition_failure(
            str(acquisition.get("status") or ""),
            str(acquisition.get("error") or ""),
        )
        if (
            recovery is None
            or not recovery.recoverable
            or not recovery.retry_same_operation
            or recovery.reason_code != "ACQUISITION_TRANSIENT_BINDERY_FAILURE"
        ):
            raise AcquisitionSafetyError(
                "The current acquisition failure no longer authorizes same-operation retry."
            )

        configured = _automation_settings()
        if not configured.automatic_reacquisition:
            raise AcquisitionSafetyError("Automatic reacquisition is disabled.")
        if not settings.allow_actions:
            raise AcquisitionSafetyError("Automatic actions are disabled.")

        result = result_by_id(int(acquisition["result_id"]))
        if not result:
            raise AcquisitionSafetyError("The acquisition's scan result no longer exists.")

        readiness = acquisition_readiness(client)
        if not readiness["ready"]:
            raise AcquisitionSafetyError(
                "Acquisition readiness failed: " + ", ".join(readiness["blockers"])
            )

        _, expected_title, expected_author = _result_and_book(result, client)
        candidate = _search_candidate(
            client,
            int(acquisition["book_id"]),
            str(acquisition.get("candidate_guid") or ""),
            expected_title,
            expected_author,
        )

        if str(candidate.get("title") or "").strip() != str(
            acquisition.get("candidate_title") or ""
        ).strip():
            raise AcquisitionSafetyError(
                "The candidate title for the stored GUID changed; retry is blocked."
            )
        stored_protocol = str(acquisition.get("candidate_protocol") or "").strip().casefold()
        current_protocol = str(candidate.get("protocol") or "").strip().casefold()
        if stored_protocol and current_protocol != stored_protocol:
            raise AcquisitionSafetyError(
                "The candidate protocol for the stored GUID changed; retry is blocked."
            )

        final_readiness = acquisition_readiness(client)
        if not final_readiness["ready"]:
            raise AcquisitionSafetyError(
                "Acquisition readiness changed during retry validation: "
                + ", ".join(final_readiness["blockers"])
            )

        reset_ebook_acquisition_for_retry(int(acquisition_id))
        try:
            response = client.grab(int(acquisition["book_id"]), candidate)
        except Exception as exc:
            update_ebook_acquisition(
                int(acquisition_id),
                "failed",
                error=f"Bindery grab failed: {exc}",
            )
            raise AcquisitionSafetyError(f"Bindery grab failed: {exc}") from exc

        queue_id = _response_queue_id(response)
        update_ebook_acquisition(
            int(acquisition_id),
            "queued",
            queue_id=queue_id,
            grab_response=_safe_grab_response(response),
        )

    return {
        "ok": True,
        "acquisition": ebook_acquisition_by_id(int(acquisition_id)),
        "message": "The same validated candidate was retried on the existing acquisition.",
    }


def _matching_queue_items(
    acquisition: dict[str, Any],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    queue_id = acquisition.get("queue_id")
    if queue_id is not None:
        exact = [
            item
            for item in items
            if str(item.get("id") or "") == str(queue_id)
        ]
        if exact:
            return exact
    return [
        item
        for item in items
        if str(item.get("bookId") or "") == str(acquisition["book_id"])
    ]


def _mark_review_required(
    acquisition_id: int,
    message: str,
) -> dict[str, Any]:
    update_ebook_acquisition(
        acquisition_id,
        "review_required",
        error=message,
    )
    return ebook_acquisition_by_id(acquisition_id) or {}


def reconcile_ebook_acquisition(
    acquisition_id: int,
    client: BinderyClient | None = None,
    *,
    expected_queue_id: int | str | None = None,
    expected_staged_fingerprint: tuple[str, int, int] | None = None,
    expected_status: str | None = None,
) -> dict[str, Any]:
    """Correlate one queue record with exactly one independently verified file."""
    client = client or BinderyClient()
    with _acquisition_lock:
        acquisition = ebook_acquisition_by_id(acquisition_id)
        if not acquisition:
            raise AcquisitionSafetyError("Acquisition record not found.")
        if expected_status is not None and acquisition.get("status") != expected_status:
            raise AcquisitionSafetyError("The durable acquisition status changed.")
        if expected_queue_id is not None and (
            str(acquisition.get("queue_id") or "") != str(expected_queue_id)
        ):
            raise AcquisitionSafetyError("The durable queue identity changed.")
        status = str(acquisition.get("status") or "")
        if status not in _RECONCILABLE_STATUSES:
            raise AcquisitionSafetyError(
                f"Acquisition status '{status}' is not eligible for reconciliation."
            )
        if status == "verified":
            return {
                "ok": True,
                "acquisition": acquisition,
                "message": "The staged ebook is already verified and awaits explicit admission.",
            }
        if not settings.allow_actions:
            raise AcquisitionSafetyError("Automatic actions are disabled.")
        configured = _automation_settings()
        if not configured.automatic_reacquisition:
            raise AcquisitionSafetyError("Automatic reacquisition is disabled.")

        try:
            topology = preimport_readiness(client)
            auto_grab = _bindery_setting_text(
                client,
                "autoGrab.enabled",
                "true",
            ).casefold()
            inventory = list_staged_ebooks(1000)
        except (PreImportSafetyError, StagingSafetyError) as exc:
            raise AcquisitionSafetyError(str(exc)) from exc
        if not topology["ready"]:
            raise AcquisitionSafetyError(
                "Pre-import readiness failed: " + ", ".join(topology["blockers"])
            )
        if auto_grab != "false":
            raise AcquisitionSafetyError("Bindery auto-grab must remain disabled.")
        if inventory["truncated"]:
            record = _mark_review_required(
                acquisition_id,
                "Staging inventory was truncated; file attribution is ambiguous.",
            )
            return {"ok": False, "acquisition": record}
        if expected_staged_fingerprint is not None:
            actual = [
                (str(item["relativePath"]), int(item["size"]), int(item["modifiedNs"]))
                for item in inventory["items"]
            ]
            if actual != [expected_staged_fingerprint]:
                raise AcquisitionSafetyError("The staged file fingerprint changed.")

        result = result_by_id(int(acquisition["result_id"]))
        if not result:
            raise AcquisitionSafetyError("The acquisition's scan result no longer exists.")
        _result_and_book(result, client)

        queue_items, queue_partial = _queue_payload(client)
        if queue_partial:
            raise AcquisitionSafetyError(
                "Bindery returned a partial queue response; reconciliation stopped."
            )
        if expected_queue_id is not None:
            exact = [
                item for item in queue_items
                if str(item.get("id") or "") == str(expected_queue_id)
            ]
            if len(exact) != 1 or (
                str(exact[0].get("bookId") or "")
                != str(acquisition["book_id"])
            ) or (
                str(exact[0].get("title") or "").strip().casefold()
                != str(acquisition.get("candidate_title") or "").strip().casefold()
            ) or (
                str(exact[0].get("protocol") or "").strip().casefold()
                != str(acquisition.get("candidate_protocol") or "").strip().casefold()
            ) or _queue_status(exact[0]) not in _AWAITING_STAGING_QUEUE_STATUSES:
                raise AcquisitionSafetyError("The exact queue handoff is no longer proven.")
            competing = [
                item for item in _active_or_unknown_queue_items(queue_items)
                if str(item.get("bookId") or "") == str(acquisition["book_id"])
                and str(item.get("id") or "") != str(expected_queue_id)
            ]
            if competing:
                raise AcquisitionSafetyError("Another active queue item shares this book.")
        matches = _matching_queue_items(acquisition, queue_items)
        if len(matches) > 1:
            record = _mark_review_required(
                acquisition_id,
                "Multiple Bindery queue records matched this acquisition."
            )
            return {"ok": False, "acquisition": record}

        queue_item = matches[0] if matches else None
        queue_status = _queue_status(queue_item) if queue_item else "absent"
        queue_id = _response_queue_id(queue_item) if queue_item else None
        if queue_status in _FAILED_QUEUE_STATUSES:
            update_ebook_acquisition(
                acquisition_id,
                "failed",
                queue_id=queue_id,
                queue_status=queue_status,
                error=str(queue_item.get("errorMessage") or "Bindery download failed."),
            )
            return {
                "ok": True,
                "acquisition": ebook_acquisition_by_id(acquisition_id),
                "message": "Bindery reported that the acquisition failed.",
            }

        if (
            inventory["items"]
            and queue_item is not None
            and queue_status not in _AWAITING_STAGING_QUEUE_STATUSES
        ):
            update_ebook_acquisition(
                acquisition_id,
                _acquisition_status_for_queue(queue_status),
                queue_id=queue_id,
                queue_status=queue_status or "unknown",
            )
            return {
                "ok": True,
                "acquisition": ebook_acquisition_by_id(acquisition_id),
                "message": (
                    "A staged path is visible, but Bindery has not completed its "
                    "external handoff; verification remains blocked."
                ),
            }

        if len(inventory["items"]) > 1:
            record = _mark_review_required(
                acquisition_id,
                "Staging contains multiple supported ebooks; file attribution is ambiguous.",
            )
            return {
                "ok": False,
                "acquisition": record,
                "stagedFiles": inventory["items"],
                "message": "Manual staging review is required; no file was admitted or removed.",
            }

        evaluated: list[dict[str, Any]] = []
        safe: list[dict[str, Any]] = []
        if inventory["items"]:
            item = inventory["items"][0]
            observed = (
                str(item["relativePath"]),
                int(item["size"]),
                int(item["modifiedNs"]),
            )
            previous = (
                str(acquisition.get("observed_relative_path") or ""),
                acquisition.get("observed_size"),
                acquisition.get("observed_modified_ns"),
            )
            if observed != previous:
                update_ebook_acquisition(
                    acquisition_id,
                    "staging_observed",
                    queue_id=queue_id,
                    queue_status=queue_status,
                    observed_relative_path=observed[0],
                    observed_size=observed[1],
                    observed_modified_ns=observed[2],
                )
                return {
                    "ok": True,
                    "acquisition": ebook_acquisition_by_id(acquisition_id),
                    "message": (
                        "One staged ebook was observed. Reconcile again after the file "
                        "has remained unchanged."
                    ),
                }

        for item in inventory["items"]:
            relative_path = str(item["relativePath"])
            try:
                verification = verify_staged_ebook(
                    int(acquisition["book_id"]),
                    relative_path,
                    client,
                )
            except Exception as exc:
                evaluated.append({
                    "relativePath": relative_path,
                    "safeToAdmit": False,
                    "error": str(exc),
                })
                continue
            evaluated.append({
                "relativePath": relative_path,
                "safeToAdmit": bool(verification.get("safeToAdmit")),
                "verdict": verification.get("verdict"),
                "confidence": verification.get("confidence"),
                "sha256": verification.get("sha256"),
            })
            if verification.get("safeToAdmit"):
                safe.append(verification)

        if len(safe) == 1:
            verification = safe[0]
            update_ebook_acquisition(
                acquisition_id,
                "verified",
                queue_id=queue_id,
                queue_status=queue_status,
                staged_relative_path=str(verification["relativePath"]),
                staged_sha256=str(verification["sha256"]),
                verification=verification,
            )
            return {
                "ok": True,
                "acquisition": ebook_acquisition_by_id(acquisition_id),
                "evaluatedStagedFiles": evaluated,
                "message": (
                    "Exactly one staged ebook independently verified. Explicit admission "
                    "is now available."
                ),
            }
        if inventory["items"]:
            record = _mark_review_required(
                acquisition_id,
                "Staging did not contain exactly one file that safely verifies for this book.",
            )
            return {
                "ok": False,
                "acquisition": record,
                "evaluatedStagedFiles": evaluated,
                "message": "Manual staging review is required; no file was admitted or removed.",
            }

        if not matches:
            update_ebook_acquisition(
                acquisition_id,
                "awaiting_staging",
                queue_status="absent",
            )
            return {
                "ok": True,
                "acquisition": ebook_acquisition_by_id(acquisition_id),
                "message": "The queue record is absent; waiting for the external staged file.",
            }

        update_ebook_acquisition(
            acquisition_id,
            _acquisition_status_for_queue(queue_status),
            queue_id=queue_id,
            queue_status=queue_status or "unknown",
        )
        return {
            "ok": True,
            "acquisition": ebook_acquisition_by_id(acquisition_id),
            "message": "Acquisition state was reconciled without modifying staged files.",
        }


def admit_ebook_acquisition(
    acquisition_id: int,
    client: BinderyClient | None = None,
    *,
    before_publish: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Hand one verified acquisition to the existing guarded admission transaction."""
    client = client or BinderyClient()
    with _acquisition_lock:
        acquisition = ebook_acquisition_by_id(acquisition_id)
        if not acquisition:
            raise AcquisitionSafetyError("Acquisition record not found.")
        if acquisition.get("status") != "verified":
            raise AcquisitionSafetyError(
                "Only a verified acquisition can be submitted for admission."
            )
        if not settings.allow_actions:
            raise AcquisitionSafetyError("Automatic actions are disabled.")
        if not _automation_settings().automatic_reacquisition:
            raise AcquisitionSafetyError("Automatic reacquisition is disabled.")

        relative_path = str(acquisition.get("staged_relative_path") or "")
        expected_hash = str(acquisition.get("staged_sha256") or "")
        try:
            _, staged_path = resolve_staged_file(relative_path)
        except StagingSafetyError as exc:
            raise AcquisitionSafetyError(str(exc)) from exc
        if not expected_hash or sha256_file(staged_path) != expected_hash:
            raise AcquisitionSafetyError(
                "The verified staged file changed before admission."
            )

        result = result_by_id(int(acquisition["result_id"]))
        if not result:
            raise AcquisitionSafetyError("The acquisition's scan result no longer exists.")
        try:
            if before_publish is None:
                admitted = admit_staged_ebook(result, relative_path, client)
            else:
                admitted = admit_staged_ebook(
                    result, relative_path, client, before_publish=before_publish,
                )
        except AdmissionSafetyError as exc:
            update_ebook_acquisition(
                acquisition_id,
                "verified",
                error=f"Admission blocked: {exc}",
            )
            raise AcquisitionSafetyError(str(exc)) from exc

        update_ebook_acquisition(
            acquisition_id,
            "admitted",
            admission_id=int(admitted["admissionId"]),
        )
        return {
            "ok": True,
            "acquisition": ebook_acquisition_by_id(acquisition_id),
            "admission": admitted,
            "message": (
                "The verified acquisition was submitted to guarded admission. "
                "The staged source remains retained."
            ),
        }


def finalize_ebook_acquisition(
    acquisition_id: int,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Remove a registered external handoff record and its verified staged copy."""
    client = client or BinderyClient()
    with _acquisition_lock:
        acquisition = ebook_acquisition_by_id(acquisition_id)
        if not acquisition:
            raise AcquisitionSafetyError("Acquisition record not found.")
        status = str(acquisition.get("status") or "")
        if status not in _FINALIZABLE_STATUSES:
            raise AcquisitionSafetyError(
                f"Acquisition status '{status}' is not eligible for finalization."
            )
        if status == "finalized":
            return {
                "ok": True,
                "acquisition": acquisition,
                "message": "The acquisition is already finalized.",
            }
        if not settings.allow_actions:
            raise AcquisitionSafetyError("Automatic actions are disabled.")
        configured = _automation_settings()
        if not configured.automatic_reacquisition:
            raise AcquisitionSafetyError("Automatic reacquisition is disabled.")

        admission_id = acquisition.get("admission_id")
        admission = ebook_admission_by_id(int(admission_id)) if admission_id else None
        if not admission or str(admission.get("status") or "") != "registered":
            raise AcquisitionSafetyError(
                "The linked admission is not registered by Bindery."
            )
        expected_hash = str(acquisition.get("staged_sha256") or "")
        relative_path = str(acquisition.get("staged_relative_path") or "")
        if (
            not expected_hash
            or expected_hash != str(admission.get("staged_sha256") or "")
            or relative_path != str(admission.get("staged_relative_path") or "")
            or int(admission.get("book_id") or 0) != int(acquisition["book_id"])
            or int(admission.get("result_id") or 0) != int(acquisition["result_id"])
        ):
            raise AcquisitionSafetyError(
                "The acquisition and registered admission records do not match."
            )

        result = result_by_id(int(acquisition["result_id"]))
        if (
            not result
            or str(result.get("stored_path") or "")
            != str(admission.get("stored_path") or "")
            or str(result.get("local_path") or "")
            != str(admission.get("local_path") or "")
        ):
            raise AcquisitionSafetyError(
                "The registered admission no longer matches its scan result."
            )
        library_path = Path(str(admission["local_path"]))
        if not library_path.is_file() or library_path.is_symlink():
            raise AcquisitionSafetyError(
                "The admitted library file is missing or symlinked."
            )
        if sha256_file(library_path) != expected_hash:
            raise AcquisitionSafetyError(
                "The admitted library file no longer matches the verified snapshot."
            )
        try:
            book = client.get_book(int(acquisition["book_id"]))
        except BinderyClientError as exc:
            raise AcquisitionSafetyError(str(exc)) from exc
        registered_path = os.path.normpath(str(admission["stored_path"]))
        registered = any(
            isinstance(item, dict)
            and str(item.get("format") or "").casefold() == "ebook"
            and os.path.normpath(str(item.get("path") or "")) == registered_path
            for item in (book.get("bookFiles") or [])
        )
        if not registered:
            raise AcquisitionSafetyError(
                "Bindery no longer registers the admitted ebook path."
            )

        queue_items, queue_partial = _queue_payload(client)
        if queue_partial:
            raise AcquisitionSafetyError(
                "Bindery returned a partial queue response; finalization stopped."
            )
        queue_id = acquisition.get("queue_id")
        if queue_id is None:
            raise AcquisitionSafetyError("The acquisition has no recorded queue ID.")
        queue_matches = [
            item
            for item in queue_items
            if str(item.get("id") or "") == str(queue_id)
        ]
        if len(queue_matches) > 1:
            raise AcquisitionSafetyError(
                "Multiple Bindery queue records share the acquisition queue ID."
            )
        queue_item = queue_matches[0] if queue_matches else None
        queue_status = _queue_status(queue_item) if queue_item else "absent"
        if queue_item and queue_status not in _FINALIZABLE_QUEUE_STATUSES:
            raise AcquisitionSafetyError(
                f"Bindery queue status '{queue_status}' is not safe to finalize."
            )

        try:
            staging_root, staged_path = resolve_staged_file(relative_path)
        except StagingSafetyError as exc:
            safely_absent = False
            if queue_item is None and status in {
                "admitted",
                "finalizing",
                "cleanup_required",
            }:
                try:
                    safely_absent = staged_path_is_absent(relative_path)
                except StagingSafetyError as recovery_exc:
                    raise AcquisitionSafetyError(str(recovery_exc)) from recovery_exc
            if safely_absent:
                update_ebook_acquisition(
                    acquisition_id,
                    "finalized",
                    queue_status="absent",
                )
                return {
                    "ok": True,
                    "acquisition": ebook_acquisition_by_id(acquisition_id),
                    "queueRecordRemoved": True,
                    "removedFromDownloadClient": False,
                    "downloadedDataDeleted": False,
                    "stagedFileRemoved": True,
                    "cleanupAlreadyComplete": True,
                    "message": (
                        "The registered acquisition was already safely cleaned; "
                        "its durable audit state is now finalized."
                    ),
                }
            raise AcquisitionSafetyError(str(exc)) from exc

        before = staged_path.stat()
        if sha256_file(staged_path) != expected_hash:
            raise AcquisitionSafetyError(
                "The verified staged file changed before finalization."
            )

        update_ebook_acquisition(
            acquisition_id,
            "finalizing",
            queue_status=queue_status,
        )
        try:
            if queue_item:
                client.remove_queue_item(
                    int(queue_id),
                    remove_from_client=False,
                    delete_files=False,
                )
                remaining, remaining_partial = _queue_payload(client)
                if remaining_partial or any(
                    str(item.get("id") or "") == str(queue_id)
                    for item in remaining
                ):
                    raise AcquisitionSafetyError(
                        "Bindery did not remove the finalized queue record."
                    )

            after = staged_path.stat()
            unchanged = (
                before.st_dev == after.st_dev
                and before.st_ino == after.st_ino
                and before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
                and before.st_ctime_ns == after.st_ctime_ns
            )
            if not unchanged or sha256_file(staged_path) != expected_hash:
                raise AcquisitionSafetyError(
                    "The staged file changed while finalization was running."
                )
            staged_path.unlink()
            directory_fd = os.open(
                staging_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

            update_ebook_acquisition(
                acquisition_id,
                "finalized",
                queue_status="removed" if queue_item else "absent",
            )
        except Exception as exc:
            update_ebook_acquisition(
                acquisition_id,
                "cleanup_required",
                error=str(exc),
            )
            if isinstance(exc, AcquisitionSafetyError):
                raise
            raise AcquisitionSafetyError(str(exc)) from exc

        return {
            "ok": True,
            "acquisition": ebook_acquisition_by_id(acquisition_id),
            "queueRecordRemoved": True,
            "removedFromDownloadClient": False,
            "downloadedDataDeleted": False,
            "stagedFileRemoved": True,
            "message": (
                "Registered admission finalized; the terminal queue record and "
                "verified staged copy were removed."
            ),
        }


def acquisition_history(limit: int = 100) -> dict[str, Any]:
    items = recent_ebook_acquisitions(limit)
    return {"items": items, "count": len(items)}
