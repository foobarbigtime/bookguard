from __future__ import annotations

from typing import Any

from .automatic_contracts import AutomaticExecutionBlocked
from .acquisition import (
    AcquisitionSafetyError, _result_and_book, _search_candidate,
    acquisition_readiness, reconcile_ebook_acquisition,
    retry_failed_ebook_acquisition,
)
from .bindery_client import BinderyClient
from .db import ebook_acquisition_by_id, local_conn, result_by_id
from .observe import _acquisition_decisions
from .recovery_classifier import classify_acquisition_failure
from .recovery_planner import _build_plan


class _TransientAcquisitionRetryExecutor:
    action_code = "retry_grab_once"

    def revalidate(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
    ) -> dict[str, Any]:
        if str(plan.get("planKind") or "") != "RETRY_ACQUISITION_TRANSIENT":
            raise AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH",
                "retry_grab_once is valid only for RETRY_ACQUISITION_TRANSIENT.",
            )
        if str(plan.get("subjectKind") or "") != "acquisition":
            raise AutomaticExecutionBlocked(
                "SUBJECT_KIND_MISMATCH",
                "Transient acquisition retry requires an acquisition subject.",
            )

        acquisition_id = int(plan["subjectId"])
        acquisition = ebook_acquisition_by_id(acquisition_id)
        if not acquisition:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The planned acquisition no longer exists.",
            )

        recovery = classify_acquisition_failure(
            str(acquisition.get("status") or ""),
            str(acquisition.get("error") or ""),
        )
        if (
            recovery is None
            or recovery.reason_code != "ACQUISITION_TRANSIENT_BINDERY_FAILURE"
            or not recovery.retry_same_operation
        ):
            raise AutomaticExecutionBlocked(
                "RECOVERY_CLASSIFICATION_CHANGED",
                "The acquisition is no longer classified as a retryable transient failure.",
            )

        with local_conn() as conn:
            decisions = _acquisition_decisions(conn, 500)
            current_decision = next(
                (
                    item
                    for item in decisions
                    if str(item.get("subjectKind") or "") == "acquisition"
                    and str(item.get("subjectId") or "") == str(acquisition_id)
                ),
                None,
            )
            current_plan = _build_plan(conn, current_decision) if current_decision else None

        if not current_plan:
            raise AutomaticExecutionBlocked(
                "DECISION_NO_LONGER_AUTHORIZED",
                "The current decision policy no longer authorizes a recovery plan.",
            )

        client = BinderyClient()
        result = result_by_id(int(acquisition["result_id"]))
        if not result:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The acquisition's scan result no longer exists.",
            )

        readiness = acquisition_readiness(client)
        if not readiness["ready"]:
            raise AutomaticExecutionBlocked(
                "ACQUISITION_READINESS_FAILED",
                "Fresh acquisition readiness failed: " + ", ".join(readiness["blockers"]),
            )

        _, expected_title, expected_author = _result_and_book(result, client)
        candidate = _search_candidate(
            client,
            int(acquisition["book_id"]),
            str(acquisition.get("candidate_guid") or ""),
            expected_title,
            expected_author,
        )
        candidate_same = (
            str(candidate.get("title") or "").strip()
            == str(acquisition.get("candidate_title") or "").strip()
            and (
                not str(acquisition.get("candidate_protocol") or "").strip()
                or str(candidate.get("protocol") or "").strip().casefold()
                == str(acquisition.get("candidate_protocol") or "").strip().casefold()
            )
        )

        checks = [
            {
                "code": "SUBJECT_IDENTITY_UNCHANGED",
                "ok": (
                    str(current_plan.get("subjectId") or "") == str(plan.get("subjectId") or "")
                    and current_plan.get("resultId") == plan.get("resultId")
                    and current_plan.get("bookId") == plan.get("bookId")
                ),
            },
            {
                "code": "PATH_UNCHANGED",
                "ok": str(current_plan.get("path") or "") == str(plan.get("path") or ""),
            },
            {
                "code": "EVIDENCE_REVISION_UNCHANGED",
                "ok": str(current_plan.get("evidenceRevision") or "")
                == str(plan.get("evidenceRevision") or ""),
            },
            {
                "code": "DECISION_STILL_AUTHORIZED",
                "ok": str(current_plan.get("signature") or "")
                == str(plan.get("signature") or ""),
            },
            {
                "code": "WORKFLOW_STATE_UNCHANGED",
                "ok": str(acquisition.get("status") or "").casefold() == "failed",
            },
            {
                "code": "RECOVERY_CLASSIFICATION_UNCHANGED",
                "ok": recovery.reason_code == str(plan.get("reasonCode") or ""),
            },
            {"code": "ACQUISITION_READINESS_CURRENT", "ok": bool(readiness["ready"])},
            {"code": "CANDIDATE_IDENTITY_UNCHANGED", "ok": candidate_same},
        ]

        return {
            "ok": all(check["ok"] for check in checks),
            "planSignature": str(plan["signature"]),
            "evidenceRevision": str(plan["evidenceRevision"]),
            "checks": checks,
            "acquisitionId": acquisition_id,
            "candidateGuid": str(acquisition.get("candidate_guid") or ""),
        }

    def reconcile_uncertain(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        """Adopt a proven remote grab after an interrupted running execution.

        A running journal row means the process may have died after Bindery
        accepted the grab. Never send the grab again. Reconcile only when the
        current Bindery queue contains exactly one item matching the durable
        book, candidate title, and protocol.
        """
        acquisition_id = int(plan["subjectId"])
        acquisition = ebook_acquisition_by_id(acquisition_id)
        if not acquisition:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The interrupted acquisition no longer exists.",
            )
        if str(acquisition.get("status") or "").casefold() != "grab_requested":
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted grab cannot be adopted because the durable acquisition "
                "is not in grab_requested state.",
            )

        client = BinderyClient()
        try:
            payload = client.list_queue()
        except Exception as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Interrupted grab outcome could not be checked safely: {exc}",
            ) from exc

        if isinstance(payload, list):
            items = [item for item in payload if isinstance(item, dict)]
            partial = False
            structurally_complete = len(items) == len(payload)
        elif isinstance(payload, dict):
            raw_items = payload.get("items")
            structurally_complete = isinstance(raw_items, list)
            items = (
                [item for item in raw_items if isinstance(item, dict)]
                if structurally_complete
                else []
            )
            structurally_complete = (
                structurally_complete and len(items) == len(raw_items)
            )
            partial = bool(payload.get("partial", False))
        else:
            items = []
            partial = False
            structurally_complete = False

        if not structurally_complete or partial:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted grab outcome cannot be adopted from a partial or invalid "
                "Bindery queue response.",
            )

        expected_book = str(acquisition.get("book_id") or "")
        expected_title = str(acquisition.get("candidate_title") or "").strip().casefold()
        expected_protocol = (
            str(acquisition.get("candidate_protocol") or "").strip().casefold()
        )
        matches = []
        for item in items:
            item_protocol = str(item.get("protocol") or "").strip().casefold()
            if (
                str(item.get("bookId") or "") == expected_book
                and str(item.get("title") or "").strip().casefold() == expected_title
                and (not expected_protocol or item_protocol == expected_protocol)
            ):
                matches.append(item)

        if len(matches) != 1 or matches[0].get("id") is None:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted grab was not proven by exactly one current Bindery queue "
                "item with the durable book, candidate title, and protocol.",
            )

        expected_queue_id = matches[0].get("id")
        try:
            reconciled = reconcile_ebook_acquisition(acquisition_id, client)
        except AcquisitionSafetyError as exc:
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                f"Interrupted grab queue proof could not be reconciled safely: {exc}",
            ) from exc

        updated = reconciled.get("acquisition") or {}
        adopted_status = str(updated.get("status") or "")
        adopted_queue_id = updated.get("queue_id")
        if (
            adopted_status
            not in {
                "queued",
                "downloading",
                "awaiting_staging",
                "staging_observed",
                "verified",
            }
            or str(adopted_queue_id or "") != str(expected_queue_id)
        ):
            raise AutomaticExecutionBlocked(
                "UNCERTAIN_EXTERNAL_OUTCOME",
                "Interrupted grab queue proof did not reconcile to the same durable "
                "queue item.",
            )

        return {
            "acquisitionId": acquisition_id,
            "status": adopted_status,
            "queueId": adopted_queue_id,
            "reconciledAfterRestart": True,
            "proof": {
                "bookId": int(acquisition["book_id"]),
                "candidateTitle": str(acquisition.get("candidate_title") or ""),
                "candidateProtocol": str(acquisition.get("candidate_protocol") or ""),
            },
        }

    def execute(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            result = retry_failed_ebook_acquisition(int(plan["subjectId"]))
        except AcquisitionSafetyError as exc:
            raise RuntimeError(str(exc)) from exc
        acquisition = result.get("acquisition") or {}
        return {
            "acquisitionId": int(plan["subjectId"]),
            "status": str(acquisition.get("status") or ""),
            "queueId": acquisition.get("queue_id"),
        }
