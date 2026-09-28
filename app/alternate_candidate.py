"""Read-only review of an explicitly selected alternate ebook release."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .acquisition import (
    AcquisitionSafetyError,
    _result_and_book,
    _search_candidate,
    acquisition_readiness,
)
from .bindery_client import BinderyClient
from .db import ebook_acquisition_by_id, result_by_id
from .recovery_classifier import classify_acquisition_failure


def _candidate_fingerprint(candidate: dict[str, Any]) -> str:
    """Digest the exact release payload without exposing its download URL."""
    identity = {
        key: candidate.get(key)
        for key in (
            "guid", "title", "nzbUrl", "size", "protocol", "mediaType",
            "indexerId", "indexerName", "indexer",
        )
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def alternate_candidate_preview(
    acquisition_id: int,
    candidate_guid: str,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Review one operator-picked alternate; never create or grab an acquisition.

    A future live executor must independently repeat these checks at its own
    mutation boundary and durably bind the selected release to the plan.
    """
    guid = str(candidate_guid or "").strip()
    if not guid or len(guid) > 4096:
        raise AcquisitionSafetyError("An alternate candidate GUID is required.")

    failed = ebook_acquisition_by_id(int(acquisition_id))
    if not failed or str(failed.get("status") or "").casefold() != "failed":
        raise AcquisitionSafetyError("A durably failed acquisition is required.")

    recovery = classify_acquisition_failure(
        str(failed["status"]), str(failed.get("error") or "")
    )
    if not recovery or recovery.reason_code != "ACQUISITION_RELEASE_ALREADY_IMPORTED":
        raise AcquisitionSafetyError(
            "The failed acquisition does not authorize an alternate release."
        )
    rejected_guid = str(failed.get("candidate_guid") or "").strip()
    rejected_title = str(failed.get("candidate_title") or "").strip()
    if not rejected_guid or not rejected_title or guid == rejected_guid:
        raise AcquisitionSafetyError(
            "The alternate must differ from the recorded rejected release."
        )
    if any(
        failed.get(key)
        for key in (
            "queue_id", "grab_response", "observed_relative_path",
            "staged_relative_path", "staged_sha256", "verification", "admission_id",
        )
    ):
        raise AcquisitionSafetyError(
            "The failed release has an uncertain queue, staging, or admission outcome."
        )

    client = client or BinderyClient()
    readiness = acquisition_readiness(client)
    if not readiness["ready"]:
        raise AcquisitionSafetyError(
            "Acquisition readiness failed: " + ", ".join(readiness["blockers"])
        )

    result = result_by_id(int(failed["result_id"]))
    if (
        not result
        or str(result.get("scan_id") or "") != str(failed.get("scan_id") or "")
        or int(result.get("book_id") or 0) != int(failed["book_id"])
    ):
        raise AcquisitionSafetyError("The failed acquisition's scan identity changed.")
    _, expected_title, expected_author = _result_and_book(result, client)
    candidate = _search_candidate(
        client, int(failed["book_id"]), guid, expected_title, expected_author
    )
    if str(candidate.get("title") or "").strip().casefold() == rejected_title.casefold():
        raise AcquisitionSafetyError(
            "The alternate has the same release title as the rejected candidate."
        )
    if not all(candidate.get(key) for key in ("guid", "title", "nzbUrl", "size")):
        raise AcquisitionSafetyError("The alternate is missing required grab fields.")

    # Store an opaque digest of the exact grab payload identity. Do not expose
    # the indexer's download URL to operators or persist it in the choice row.
    candidate_fingerprint = _candidate_fingerprint(candidate)

    # This is a point-in-time review, not authorization for any later mutation.
    return {
        "safeForReview": True,
        "liveGrabEnabled": False,
        "acquisitionId": int(failed["id"]),
        "resultId": int(result["id"]),
        "bookId": int(failed["book_id"]),
        "rejectedGuid": rejected_guid,
        "candidateFingerprint": candidate_fingerprint,
        "candidate": {
            "guid": guid,
            "title": str(candidate["title"]),
            "protocol": str(candidate.get("protocol") or ""),
            "indexer": str(candidate.get("indexerName") or candidate.get("indexer") or ""),
        },
        "message": (
            "The selected alternate passed this read-only review. No grab or "
            "admission was attempted; a future live step must revalidate it."
        ),
    }
