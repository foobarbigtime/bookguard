"""Immutable operator choice for a proven quarantined ebook, without a grab."""

from __future__ import annotations

from typing import Any

from . import automatic_execution as core
from .acquisition import (
    AcquisitionSafetyError, _result_and_book, _search_candidate,
)
from .alternate_candidate import _candidate_fingerprint
from .bindery_client import BinderyClient
from .db import local_conn, result_by_id, utc_now
from .quarantine_replacement import quarantine_replacement_preview
from .recovery_planner import recovery_plan_by_id


def quarantine_candidate_preview(
    plan_id: int, candidate_guid: str, client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Review a single fresh release against proven quarantine custody."""
    guid = str(candidate_guid or "").strip()
    if not guid or len(guid) > 4096:
        raise AcquisitionSafetyError("An explicit candidate GUID is required.")
    try:
        custody = quarantine_replacement_preview(plan_id, client)
    except ValueError as exc:
        raise AcquisitionSafetyError(str(exc)) from exc
    if not custody["safeForCandidateReview"]:
        raise AcquisitionSafetyError("The exact quarantine custody is no longer proven.")
    plan = recovery_plan_by_id(int(plan_id))
    result = result_by_id(int(custody["resultId"]))
    if not plan or not result:
        raise AcquisitionSafetyError("The current quarantine plan or result is missing.")
    client = client or BinderyClient()
    _, title, author = _result_and_book(result, client)
    candidate = _search_candidate(client, int(result["book_id"]), guid, title, author)
    if not all(candidate.get(key) for key in (
        "guid", "title", "nzbUrl", "size", "protocol",
    )):
        raise AcquisitionSafetyError("The release lacks required grab identity fields.")
    return {
        "safeForReview": True,
        "liveGrabEnabled": False,
        "planId": int(plan["id"]),
        "planSignature": str(plan["signature"]),
        "evidenceRevision": str(plan["evidenceRevision"]),
        "resultId": int(result["id"]),
        "bookId": int(result["book_id"]),
        "candidateFingerprint": _candidate_fingerprint(candidate),
        "candidate": {
            "guid": guid,
            "title": str(candidate["title"]),
            "protocol": str(candidate["protocol"]),
            "indexer": str(candidate.get("indexerName") or candidate.get("indexer") or ""),
        },
        "message": "Review only; no replacement release was grabbed or admitted.",
    }


def _selection(row, *, current: bool) -> dict[str, Any]:
    return {
        "resultId": int(row["result_id"]),
        "planId": int(row["plan_id"]),
        "planSignature": str(row["plan_signature"]),
        "evidenceRevision": str(row["evidence_revision"]),
        "quarantineExecutionId": int(row["quarantine_execution_id"]),
        "quarantineSha256": str(row["quarantine_sha256"]),
        "candidate": {
            "guid": str(row["candidate_guid"]),
            "title": str(row["candidate_title"]),
            "protocol": str(row["candidate_protocol"]),
            "indexer": str(row["candidate_indexer"]),
        },
        "candidateFingerprint": str(row["candidate_fingerprint"]),
        "selectedAt": str(row["selected_at"]),
        "currentPlan": current,
        "liveGrabEnabled": False,
    }


def quarantine_selection_by_result(result_id: int) -> dict[str, Any] | None:
    """Return the durable choice, including whether quarantine custody still holds."""
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM quarantine_replacement_selections WHERE result_id=?",
            (int(result_id),),
        ).fetchone()
    if not row:
        return None
    plan = recovery_plan_by_id(int(row["plan_id"]))
    current = False
    if plan and plan.get("signature") == row["plan_signature"]:
        custody = quarantine_replacement_preview(int(row["plan_id"]))
        receipt = core._existing(
            str(row["plan_signature"]), "quarantine_exact_media", 2,
        )
        current = bool(
            custody["safeForCandidateReview"]
            and receipt and receipt["id"] == row["quarantine_execution_id"]
            and (receipt.get("externalResult") or {}).get("sha256")
            == row["quarantine_sha256"]
        )
    return _selection(row, current=current)


def bind_quarantine_candidate(
    plan_id: int, candidate_guid: str, client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Persist one exact choice; a separate future executor must revalidate it."""
    preview = quarantine_candidate_preview(plan_id, candidate_guid, client)
    receipt = core._existing(preview["planSignature"], "quarantine_exact_media", 2)
    if not receipt or receipt.get("state") != "succeeded":
        raise AcquisitionSafetyError("The quarantine execution receipt changed.")
    sha = str((receipt.get("externalResult") or {}).get("sha256") or "")
    candidate = preview["candidate"]

    with local_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = recovery_plan_by_id(int(plan_id))
        current_receipt = core._existing(
            preview["planSignature"], "quarantine_exact_media", 2,
        )
        if (
            not plan or plan.get("signature") != preview["planSignature"]
            or plan.get("evidenceRevision") != preview["evidenceRevision"]
            or plan.get("state") != "ready" or int(plan.get("currentStep") or 0) != 3
            or not current_receipt or current_receipt.get("id") != receipt.get("id")
            or current_receipt.get("state") != "succeeded"
            or str((current_receipt.get("externalResult") or {}).get("sha256") or "") != sha
        ):
            raise AcquisitionSafetyError("The quarantine plan or receipt changed.")
        existing = conn.execute(
            "SELECT * FROM quarantine_replacement_selections WHERE result_id=?",
            (preview["resultId"],),
        ).fetchone()
        if existing:
            if (
                existing["plan_signature"] != preview["planSignature"]
                or existing["candidate_guid"] != candidate["guid"]
                or existing["candidate_fingerprint"] != preview["candidateFingerprint"]
                or existing["quarantine_execution_id"] != receipt["id"]
                or existing["quarantine_sha256"] != sha
            ):
                raise AcquisitionSafetyError(
                    "This quarantined result already has a different or stale choice."
                )
            return _selection(existing, current=True)
        conn.execute(
            """INSERT INTO quarantine_replacement_selections (
                result_id, plan_id, plan_signature, evidence_revision,
                quarantine_execution_id, quarantine_sha256,
                candidate_guid, candidate_title, candidate_protocol,
                candidate_indexer, candidate_fingerprint, selected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                preview["resultId"], int(plan_id), preview["planSignature"],
                preview["evidenceRevision"], int(receipt["id"]), sha,
                candidate["guid"], candidate["title"], candidate["protocol"],
                candidate["indexer"], preview["candidateFingerprint"], utc_now(),
            ),
        )
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM quarantine_replacement_selections WHERE result_id=?",
            (preview["resultId"],),
        ).fetchone()
        return _selection(saved, current=True)
