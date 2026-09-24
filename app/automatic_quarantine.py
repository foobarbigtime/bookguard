from __future__ import annotations

from typing import Any

from .automatic import (
    AutomaticMaintenanceError, quarantine_unsafe_media, unsafe_media_preview,
)
from .automatic_contracts import AutomaticExecutionBlocked
from .bindery_client import BinderyClient
from .db import local_conn, result_by_id
from .observe import _result_decisions
from .recovery_planner import _build_plan
from .verifier import verify_result


class _UnsafeMediaQuarantineExecutor:
    action_code = "quarantine_exact_media"

    def revalidate(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
    ) -> dict[str, Any]:
        if str(plan.get("planKind") or "") != "QUARANTINE_UNSAFE_MEDIA":
            raise AutomaticExecutionBlocked(
                "PLAN_KIND_MISMATCH",
                "quarantine_exact_media is valid only for QUARANTINE_UNSAFE_MEDIA.",
            )
        if str(plan.get("subjectKind") or "") != "result":
            raise AutomaticExecutionBlocked(
                "SUBJECT_KIND_MISMATCH",
                "Unsafe-media quarantine requires a scan-result subject.",
            )

        result_id = int(plan["subjectId"])
        result = result_by_id(result_id)
        if not result:
            raise AutomaticExecutionBlocked(
                "SUBJECT_IDENTITY_CHANGED",
                "The planned scan result no longer exists.",
            )

        try:
            verification = verify_result(result, force=True)
        except Exception as exc:
            raise AutomaticExecutionBlocked(
                "UNSAFE_REVALIDATION_ERROR",
                f"Deterministic unsafe-media verification failed: {exc}",
            ) from exc

        current_unsafe = all((
            str(verification.get("verdict") or "") == "UNSAFE_FILE",
            int(verification.get("confidence") or 0) == 100,
            str(verification.get("source") or "") == "deterministic-safety",
        ))

        with local_conn() as conn:
            decisions = _result_decisions(conn, 500)
            current_decision = next(
                (
                    item
                    for item in decisions
                    if str(item.get("subjectKind") or "") == "result"
                    and str(item.get("subjectId") or "") == str(result_id)
                ),
                None,
            )
            current_plan = _build_plan(conn, current_decision) if current_decision else None

        if not current_plan:
            raise AutomaticExecutionBlocked(
                "DECISION_NO_LONGER_AUTHORIZED",
                "The current decision policy no longer authorizes unsafe-media quarantine.",
            )

        try:
            preview = unsafe_media_preview(result, BinderyClient())
        except AutomaticMaintenanceError as exc:
            raise AutomaticExecutionBlocked(
                "UNSAFE_QUARANTINE_PREFLIGHT_FAILED",
                str(exc),
            ) from exc

        preview_checks = dict(preview.get("checks") or {})
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
                "code": "UNSAFE_VERDICT_CURRENT",
                "ok": current_unsafe,
            },
            {
                "code": "EXACT_BINDERY_ASSOCIATION_CURRENT",
                "ok": bool(preview_checks.get("exactBinderyAssociation")),
            },
            {
                "code": "SINGLE_ASSOCIATION_CURRENT",
                "ok": bool(preview_checks.get("singleAssociation")),
            },
            {
                "code": "SOURCE_BYTES_UNCHANGED",
                "ok": bool(preview_checks.get("sourceHashMatchesVerification")),
            },
            {
                "code": "WRITABLE_ALIAS_SAME_FILE",
                "ok": bool(preview_checks.get("writableAliasReady")),
            },
            {
                "code": "BINDERY_TRACKS_EXACT_PATH",
                "ok": bool(preview_checks.get("binderyTracksPath")),
            },
        ]

        return {
            "ok": all(check["ok"] for check in checks),
            "planSignature": str(plan["signature"]),
            "evidenceRevision": str(plan["evidenceRevision"]),
            "checks": checks,
            "resultId": result_id,
            "fileId": int(result["file_id"]),
            "bookId": int(result["book_id"]),
            "storedPath": str(result["stored_path"]),
            "localPath": str(result["local_path"]),
            "expectedSha256": str(preview.get("expectedSha256") or ""),
        }

    def execute(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any]:
        result = result_by_id(int(plan["subjectId"]))
        if not result:
            raise RuntimeError("The unsafe-media result disappeared before execution.")
        try:
            outcome = quarantine_unsafe_media(result, BinderyClient())
        except AutomaticMaintenanceError as exc:
            raise RuntimeError(str(exc)) from exc
        return {
            "resultId": int(plan["subjectId"]),
            "fileId": int(result["file_id"]),
            "bookId": int(result["book_id"]),
            "status": "quarantined",
            "quarantinePath": str(outcome.get("quarantinePath") or ""),
            "sha256": str(outcome.get("sha256") or ""),
            "binderyDetached": bool(outcome.get("binderyDetached")),
            "permanentDeletion": False,
            "replacementRequested": False,
        }
