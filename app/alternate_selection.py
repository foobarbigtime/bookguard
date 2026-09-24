"""Durable operator intent for one alternate candidate; no external mutation."""

from __future__ import annotations

from typing import Any

from .acquisition import AcquisitionSafetyError
from .alternate_candidate import alternate_candidate_preview
from .bindery_client import BinderyClient
from .db import local_conn, utc_now
from .observe import _acquisition_decisions
from .recovery_planner import _build_plan, recovery_plan_by_id


def _current_plan(conn, acquisition_id: int) -> dict[str, Any] | None:
    decision = next(
        (
            item for item in _acquisition_decisions(conn, 500)
            if item["subjectKind"] == "acquisition"
            and item["subjectId"] == str(acquisition_id)
        ),
        None,
    )
    return _build_plan(conn, decision) if decision else None


def _selection(row, current_plan: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "acquisitionId": int(row["acquisition_id"]),
        "planId": int(row["plan_id"]),
        "planSignature": str(row["plan_signature"]),
        "evidenceRevision": str(row["evidence_revision"]),
        "candidate": {
            "guid": str(row["candidate_guid"]),
            "title": str(row["candidate_title"]),
            "protocol": str(row["candidate_protocol"]),
            "indexer": str(row["candidate_indexer"]),
        },
        "candidateFingerprint": str(row["candidate_fingerprint"]),
        "selectedAt": str(row["selected_at"]),
        "currentPlan": bool(
            current_plan and current_plan["signature"] == row["plan_signature"]
        ),
        "liveGrabEnabled": False,
    }


def alternate_selection_by_acquisition(acquisition_id: int) -> dict[str, Any] | None:
    """Read the saved choice and indicate whether its E3 plan still matches."""
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM alternate_candidate_selections WHERE acquisition_id=?",
            (int(acquisition_id),),
        ).fetchone()
        return _selection(row, _current_plan(conn, acquisition_id)) if row else None


def bind_alternate_candidate(
    plan_id: int,
    candidate_guid: str,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Save one explicit choice against the current E3 plan, without a grab.

    A different choice for the same failed acquisition is refused. A later
    executor must check both the stored plan and fresh candidate fingerprint.
    """
    plan = recovery_plan_by_id(int(plan_id))
    if (
        not plan
        or plan["planKind"] != "SELECT_ALTERNATE_REPLACEMENT"
        or plan["subjectKind"] != "acquisition"
        or plan["state"] not in {"planned", "ready"}
        or int(plan["currentStep"]) > 3
    ):
        raise AcquisitionSafetyError("A current alternate-replacement plan is required.")

    acquisition_id = int(plan["subjectId"])
    with local_conn() as conn:
        current = _current_plan(conn, acquisition_id)
    if not current or current["signature"] != plan["signature"]:
        raise AcquisitionSafetyError("The alternate-replacement plan is stale.")

    preview = alternate_candidate_preview(acquisition_id, candidate_guid, client)
    if (
        preview["acquisitionId"] != acquisition_id
        or preview["resultId"] != plan["resultId"]
        or preview["bookId"] != plan["bookId"]
    ):
        raise AcquisitionSafetyError("The alternate preview changed subject identity.")

    candidate = preview["candidate"]
    with local_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        persisted = conn.execute(
            "SELECT signature, state, current_step FROM recovery_plans WHERE id=?",
            (int(plan_id),),
        ).fetchone()
        current = _current_plan(conn, acquisition_id)
        if (
            not persisted
            or persisted["signature"] != plan["signature"]
            or persisted["state"] not in {"planned", "ready"}
            or int(persisted["current_step"]) > 3
            or not current
            or current["signature"] != plan["signature"]
        ):
            raise AcquisitionSafetyError("The alternate-replacement plan changed.")

        existing = conn.execute(
            "SELECT * FROM alternate_candidate_selections WHERE acquisition_id=?",
            (acquisition_id,),
        ).fetchone()
        if existing:
            if (
                existing["plan_signature"] != plan["signature"]
                or existing["candidate_guid"] != candidate["guid"]
                or existing["candidate_fingerprint"] != preview["candidateFingerprint"]
            ):
                raise AcquisitionSafetyError(
                    "This failed acquisition already has a different or stale "
                    "alternate choice; automatic replacement is blocked."
                )
            return _selection(existing, current)

        conn.execute(
            """
            INSERT INTO alternate_candidate_selections (
                acquisition_id, plan_id, plan_signature, evidence_revision,
                candidate_guid, candidate_title, candidate_protocol,
                candidate_indexer, candidate_fingerprint, selected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                acquisition_id, int(plan_id), plan["signature"],
                plan["evidenceRevision"], candidate["guid"], candidate["title"],
                candidate["protocol"], candidate["indexer"],
                preview["candidateFingerprint"], utc_now(),
            ),
        )
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM alternate_candidate_selections WHERE acquisition_id=?",
            (acquisition_id,),
        ).fetchone()
        return _selection(saved, current)
