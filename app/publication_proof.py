"""Bounded filesystem proof for failed admission publication; never publishes an ebook."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from . import automatic_execution as core
from .admission import (
    AdmissionSafetyError, _book_has_ebook, _destination_for_result,
    _exact_ebook_associations, _publish_no_replace, _result_matches_book,
    _verified_staged_source, admission_readiness,
)
from .bindery_client import BinderyClient, BinderyClientError
from .db import ebook_admission_by_id, local_conn, result_by_id
from .observe import _admission_decisions
from .recovery_planner import _build_plan
from .staging import StagingSafetyError


_READ_ONLY_STEPS = (
    "revalidate_failed_admission", "revalidate_staged_bytes",
    "revalidate_admission_topology",
)


class _PublicationPrimitiveProofExecutor:
    action_code = "prove_supported_no_replace_method"

    def revalidate(self, plan: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if (plan.get("planKind"), plan.get("subjectKind"), plan.get("reasonCode")) != (
            "RECOVER_ADMISSION_PUBLICATION", "admission",
            "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED",
        ) or step.get("code") not in (*_READ_ONLY_STEPS, self.action_code):
            raise core.AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH", "Only the exact failed-publication proof is enabled."
            )
        admission_id = int(plan["subjectId"])
        admission = ebook_admission_by_id(admission_id)
        result = result_by_id(int((admission or {}).get("result_id") or 0))
        with local_conn() as conn:
            decision = next((item for item in _admission_decisions(conn, 500)
                             if item.get("subjectKind") == "admission"
                             and str(item.get("subjectId")) == str(admission_id)), None)
            current = _build_plan(conn, decision) if decision else None

        try:
            client = BinderyClient()
            readiness = admission_readiness(client)
            staged = _verified_staged_source(admission) if admission else None
            destination, stored_path = (
                _destination_for_result(result, staged) if result and staged else (None, "")
            )
            book = client.get_book(int(plan["bookId"]))
            associations = _exact_ebook_associations(stored_path) if stored_path else []
        except (
            AdmissionSafetyError, BinderyClientError, OSError, ValueError,
            sqlite3.Error, StagingSafetyError,
        ) as exc:
            raise core.AutomaticExecutionBlocked(
                "PUBLICATION_PREFLIGHT_FAILED", str(exc)
            ) from exc

        verification = (admission or {}).get("verification") or {}
        if not isinstance(verification, dict):
            verification = {}
        try:
            book_matches = bool(result and _result_matches_book(result, book))
        except StagingSafetyError as exc:
            raise core.AutomaticExecutionBlocked(
                "BINDERY_IDENTITY_UNPROVEN", str(exc)
            ) from exc
        try:
            parent_identity = destination.parent.stat() if destination else None
        except OSError as exc:
            raise core.AutomaticExecutionBlocked(
                "DESTINATION_PARENT_UNAVAILABLE", str(exc)
            ) from exc
        checks = [
            {"code": "DECISION_STILL_AUTHORIZED", "ok": bool(
                current and current.get("signature") == plan.get("signature")
                and current.get("evidenceRevision") == plan.get("evidenceRevision")
            )},
            {"code": "SUBJECT_IDENTITY_UNCHANGED", "ok": bool(
                admission and result and admission.get("result_id") == plan.get("resultId")
                and admission.get("book_id") == plan.get("bookId")
                and result.get("book_id") == plan.get("bookId")
                and result.get("id") == plan.get("resultId")
                and admission.get("stored_path") == result.get("stored_path")
                and admission.get("local_path") == result.get("local_path")
            )},
            {"code": "WORKFLOW_STATE_UNCHANGED", "ok": bool(
                admission and admission.get("status") == "failed"
                and admission.get("failure_stage") == "no_replace_unsupported"
                and not admission.get("publication_method")
            )},
            {"code": "VERIFIED_BYTES_UNCHANGED", "ok": bool(
                staged and admission.get("staged_sha256")
                and verification.get("sha256") == admission.get("staged_sha256")
                and verification.get("safeToAdmit") is True
            )},
            {"code": "PATH_UNCHANGED", "ok": bool(
                result and stored_path == plan.get("path")
                and stored_path == admission.get("stored_path")
                and destination and not destination.exists() and not destination.is_symlink()
            )},
            {"code": "ADMISSION_READINESS", "ok": readiness["ready"]},
            {"code": "BINDERY_IDENTITY_UNCHANGED", "ok": bool(
                book_matches and not _book_has_ebook(book)
            )},
            {"code": "NO_EXISTING_OWNER", "ok": not associations},
        ]
        return {
            "ok": all(item["ok"] for item in checks),
            "checks": checks,
            "planSignature": plan["signature"],
            "evidenceRevision": plan["evidenceRevision"],
            "destinationParent": str(destination.parent) if destination else "",
            "parentDevice": parent_identity.st_dev if parent_identity else None,
            "parentInode": parent_identity.st_ino if parent_identity else None,
            "stagedSha256": str((admission or {}).get("staged_sha256") or ""),
        }

    def execute(
        self, plan: dict[str, Any], step: dict[str, Any], boundary: dict[str, Any]
    ) -> dict[str, Any]:
        # A proof never grants publication authority; recheck immediately before writing.
        fresh = self.revalidate(plan, step)
        core._require_fresh_boundary(plan, fresh)
        if tuple(fresh.get(key) for key in (
            "destinationParent", "parentDevice", "parentInode", "stagedSha256"
        )) != tuple(boundary.get(key) for key in (
            "destinationParent", "parentDevice", "parentInode", "stagedSha256"
        )):
            raise RuntimeError("Admission topology or verified bytes changed before proof.")

        with tempfile.TemporaryDirectory(
            prefix=".bookguard-publication-proof-", dir=fresh["destinationParent"]
        ) as directory:
            private = Path(directory)
            source = private / "private"
            occupied = private / "occupied"
            source.write_bytes(b"bookguard-proof-source")
            occupied.write_bytes(b"bookguard-proof-occupied")
            try:
                _publish_no_replace(source, occupied)
            except FileExistsError:
                pass
            else:
                raise RuntimeError("No-replace primitive overwrote an occupied target.")
            if occupied.read_bytes() != b"bookguard-proof-occupied":
                raise RuntimeError("No-replace proof altered an occupied target.")
            if source.read_bytes() != b"bookguard-proof-source":
                raise RuntimeError("No-replace proof altered its private source.")
            method = _publish_no_replace(source, private / "published")
            if (private / "published").read_bytes() != b"bookguard-proof-source":
                raise RuntimeError("No-replace proof published unexpected bytes.")
        return {
            "admissionId": int(plan["subjectId"]), "publicationMethod": method,
            "externalMutationPerformed": True, "libraryBytesChanged": False,
            "admissionPublished": False, "binderyScanRequested": False,
        }


_EXECUTOR = _PublicationPrimitiveProofExecutor()


def register_executor() -> None:
    core.register_automatic_executor(_EXECUTOR.action_code, _EXECUTOR)


def run_publication_proof_cycle(plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = int(plan["id"])
    for code in _READ_ONLY_STEPS:
        current = core.recovery_plan_by_id(plan_id) or plan
        index = int(current.get("currentStep") or 0)
        steps = list(current.get("steps") or [])
        if index >= len(steps) or steps[index].get("code") != code:
            continue
        try:
            core._require_fresh_boundary(current, _EXECUTOR.revalidate(current, steps[index]))
        except core.AutomaticExecutionBlocked as exc:
            blocked = core.block_recovery_plan(plan_id, str(exc))
            return {"ok": False, "state": "blocked", "plan": blocked,
                    "reasonCode": exc.reason_code, "externalMutationAttempted": False}
        plan = core.record_recovery_step_success(plan_id, index)

    current = core.recovery_plan_by_id(plan_id) or plan
    index = int(current.get("currentStep") or 0)
    steps = list(current.get("steps") or [])
    if index >= len(steps) or steps[index].get("code") != _EXECUTOR.action_code:
        return {"ok": True, "state": "paused", "plan": current,
                "externalMutationAttempted": False,
                "message": "Publication and Bindery scanning remain disabled."}
    try:
        result = core.attempt_automatic_step(plan_id)
    except core.AutomaticExecutionBlocked as exc:
        if exc.reason_code in {"ACTION_NOT_ALLOWLISTED", "EXECUTOR_NOT_REGISTERED"}:
            raise
        blocked = core.block_recovery_plan(plan_id, str(exc))
        return {"ok": False, "state": "blocked", "plan": blocked,
                "reasonCode": exc.reason_code,
                "externalMutationAttempted": exc.reason_code == "EXECUTION_FAILED"}
    return {**result, "state": "replayed" if result.get("replayed") else "executed",
            "externalMutationAttempted": not result.get("replayed")}
