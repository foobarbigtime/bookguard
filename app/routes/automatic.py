from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..acquisition import (
    AcquisitionSafetyError,
    acquisition_history,
    acquisition_readiness,
    admit_ebook_acquisition,
    finalize_ebook_acquisition,
    reconcile_ebook_acquisition,
    start_ebook_acquisition,
)
from ..acquisition_coordinator import acquisition_coordinator_status
from ..alternate_candidate import alternate_candidate_preview
from ..alternate_selection import (
    alternate_selection_by_acquisition,
    bind_alternate_candidate,
)
from ..admission import (
    AdmissionSafetyError,
    admission_history,
    admission_readiness,
    admit_staged_ebook,
    correct_registration_conflict,
    reconcile_admission,
)
from ..automatic import AutomaticMaintenanceError, remediate_wrong_content, wrong_content_preview
from ..automatic_runner import (
    AutomaticExecutionBlocked,
    automatic_execution_history,
    execution_policy_snapshot,
    run_automatic_cycle,
)
from ..bindery_client import BinderyClient, BinderyClientError, evaluate_replacement_candidate
from ..config import ConfigurationError, load_automation_settings
from ..db import result_by_id
from ..observe import observe_snapshot, run_observe_cycle
from ..preimport import PreImportSafetyError, preimport_readiness
from ..staging import StagingSafetyError, list_staged_ebooks, verify_staged_ebook
from .models import ConfirmationRequest, require_confirmation


router = APIRouter(prefix="/api/automatic", tags=["automatic maintenance"])


def _require_mutation_mode() -> None:
    """Keep legacy confirmed mutation endpoints manual-only.

    Observe remains non-mutating. E4 Automatic Mode may mutate only through the
    dedicated recovery executor, never by falling through these manual routes.
    """
    try:
        configured = load_automation_settings()
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if configured.automation_mode != "manual":
        label = "Observe Mode" if configured.automation_mode == "observe" else "Automatic Mode"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{label} is active. Legacy automatic-maintenance mutation endpoints "
                "are manual-only; live E4 work must use the supervised recovery executor."
            ),
        )


class StagedVerificationRequest(BaseModel):
    relativePath: str = Field(min_length=1, max_length=4096)


class StagedAdmissionRequest(StagedVerificationRequest):
    confirm: str = Field(min_length=1, max_length=64)


class EbookAcquisitionRequest(BaseModel):
    candidateGuid: str = Field(min_length=1, max_length=4096)
    confirm: str = Field(min_length=1, max_length=64)


@router.get("/observe")
def api_automatic_observe(limit: int = 100):
    """Return durable Observe Mode decisions without running a new cycle."""
    try:
        return observe_snapshot(limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/observe/run")
def api_automatic_observe_run(payload: ConfirmationRequest):
    """Record what Automatic Mode would do without invoking mutation workflows."""
    require_confirmation(payload, "RUN_OBSERVE_MODE")
    try:
        result = run_observe_cycle()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not result.get("enabled"):
        raise HTTPException(status_code=409, detail=str(result.get("message") or "Observe Mode is disabled."))
    return result


@router.post("/run")
def api_automatic_run(payload: ConfirmationRequest):
    """Advance one supervised E4 work item and at most one external mutation."""
    require_confirmation(payload, "RUN_AUTOMATIC_CYCLE")
    try:
        return run_automatic_cycle()
    except AutomaticExecutionBlocked as exc:
        raise HTTPException(
            status_code=409,
            detail={"reasonCode": exc.reason_code, "message": str(exc)},
        )


@router.get("/execution-policy")
def api_automatic_execution_policy():
    """Read-only E4 mode, allowlist, and executor registration status."""
    try:
        return execution_policy_snapshot()
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/executions")
def api_automatic_executions(limit: int = 100):
    """Return the durable E4 execution journal without starting any work."""
    return automatic_execution_history(limit)


@router.get("/bindery-status")
def api_automatic_bindery_status():
    """Read-only connectivity check for the Automatic Maintenance pipeline."""
    try:
        status = BinderyClient().system_status()
    except BinderyClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"ok": True, "bindery": status}


@router.get("/preimport-readiness")
def api_automatic_preimport_readiness():
    """Read-only gate for the external-import staging topology.

    Automatic grabbing remains blocked unless Bindery is handing completed
    downloads to a BookGuard-visible staging folder outside the managed library.
    """
    try:
        return preimport_readiness()
    except PreImportSafetyError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/staging/files")
def api_automatic_staging_files(limit: int = 500):
    """List verification-supported staged ebooks without reading their contents."""
    try:
        return list_staged_ebooks(limit)
    except StagingSafetyError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/books/{book_id}/staged-verification")
def api_automatic_staged_verification(book_id: int, payload: StagedVerificationRequest):
    """Verify staged bytes against one explicit Bindery book without admitting them."""
    try:
        return verify_staged_ebook(book_id, payload.relativePath)
    except BinderyClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except StagingSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/admission-readiness")
def api_automatic_admission_readiness():
    """Report whether the separate writable admission path is safe and enabled."""
    try:
        return admission_readiness()
    except AdmissionSafetyError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/acquisition-readiness")
def api_automatic_acquisition_readiness():
    """Report whether one controlled Bindery acquisition may be started."""
    try:
        return acquisition_readiness()
    except AcquisitionSafetyError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/acquisitions")
def api_automatic_acquisitions(limit: int = 100):
    """Return durable queue-to-staging acquisition history."""
    return acquisition_history(limit)


@router.get("/acquisitions/{acquisition_id}/alternate-preview")
def api_automatic_alternate_preview(
    acquisition_id: int,
    candidate_guid: str = Query(min_length=1, max_length=4096),
):
    """Review one explicit alternate without requesting a Bindery grab."""
    try:
        return alternate_candidate_preview(acquisition_id, candidate_guid)
    except AcquisitionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/acquisitions/{acquisition_id}/alternate-selection")
def api_automatic_alternate_selection(acquisition_id: int):
    """Inspect an operator choice and whether its recovery plan is still current."""
    selected = alternate_selection_by_acquisition(acquisition_id)
    if selected is None:
        raise HTTPException(status_code=404, detail="No alternate candidate is selected.")
    return selected


@router.post("/plans/{plan_id}/alternate-selection")
def api_automatic_bind_alternate(plan_id: int, payload: EbookAcquisitionRequest):
    """Persist one explicit choice without requesting a Bindery grab."""
    require_confirmation(payload, "SELECT_ALTERNATE_CANDIDATE")
    try:
        return bind_alternate_candidate(plan_id, payload.candidateGuid)
    except AcquisitionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/acquisition-coordinator")
def api_automatic_acquisition_coordinator():
    """Return the read-only supervised coordinator lifecycle status."""
    return acquisition_coordinator_status()


@router.post("/results/{result_id}/acquisitions")
def api_automatic_start_acquisition(
    result_id: int,
    payload: EbookAcquisitionRequest,
):
    """Freshly revalidate and grab one explicitly selected ebook release."""
    _require_mutation_mode()
    require_confirmation(payload, "START_EBOOK_ACQUISITION")
    item = _automatic_result(result_id)
    try:
        return start_ebook_acquisition(item, payload.candidateGuid)
    except AcquisitionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/acquisitions/{acquisition_id}/reconcile")
def api_automatic_reconcile_acquisition(
    acquisition_id: int,
    payload: ConfirmationRequest,
):
    """Observe Bindery and verify an unambiguous staged ebook."""
    _require_mutation_mode()
    require_confirmation(payload, "RECONCILE_EBOOK_ACQUISITION")
    try:
        return reconcile_ebook_acquisition(acquisition_id)
    except AcquisitionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/acquisitions/{acquisition_id}/admit")
def api_automatic_admit_acquisition(
    acquisition_id: int,
    payload: ConfirmationRequest,
):
    """Submit one verified acquisition to guarded direct admission."""
    _require_mutation_mode()
    require_confirmation(payload, "ADMIT_EBOOK_ACQUISITION")
    try:
        return admit_ebook_acquisition(acquisition_id)
    except AcquisitionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/acquisitions/{acquisition_id}/finalize")
def api_automatic_finalize_acquisition(
    acquisition_id: int,
    payload: ConfirmationRequest,
):
    """Finalize a registered admission without deleting download-client data."""
    _require_mutation_mode()
    require_confirmation(payload, "FINALIZE_EBOOK_ACQUISITION")
    try:
        return finalize_ebook_acquisition(acquisition_id)
    except AcquisitionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/admissions")
def api_automatic_admissions(limit: int = 100):
    """Return durable admission history and recovery state."""
    return admission_history(limit)


@router.post("/results/{result_id}/admit-staged-ebook")
def api_automatic_admit_staged_ebook(result_id: int, payload: StagedAdmissionRequest):
    """Verify and atomically publish one staged ebook to its former path."""
    _require_mutation_mode()
    require_confirmation(payload, "ADMIT_STAGED_EBOOK")
    item = _automatic_result(result_id)
    try:
        return admit_staged_ebook(item, payload.relativePath)
    except AdmissionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/admissions/{admission_id}/reconcile")
def api_automatic_reconcile_admission(
    admission_id: int,
    payload: ConfirmationRequest,
):
    """Confirm registration, stop on a wrong owner, or request another scan."""
    _require_mutation_mode()
    require_confirmation(payload, "RECONCILE_ADMISSION")
    try:
        return reconcile_admission(admission_id)
    except AdmissionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/admissions/{admission_id}/correct-registration")
def api_automatic_correct_registration(
    admission_id: int,
    payload: ConfirmationRequest,
):
    """Explicitly correct one proven exact-path Bindery ownership conflict."""
    _require_mutation_mode()
    require_confirmation(payload, "CORRECT_BINDERY_REGISTRATION")
    try:
        return correct_registration_conflict(admission_id)
    except AdmissionSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/books/{book_id}/replacement-preview")
def api_automatic_replacement_preview(book_id: int):
    """Search Bindery and independently gate candidates without grabbing anything."""
    client = BinderyClient()
    try:
        book = client.get_book(book_id)
        search = client.search_book(book_id)
    except BinderyClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    expected_title = str(book.get("title") or "")
    author_obj = book.get("author") if isinstance(book.get("author"), dict) else {}
    expected_author = str(
        book.get("authorName")
        or author_obj.get("name")
        or author_obj.get("authorName")
        or ""
    )

    evaluated = []
    safe_count = 0
    for result in search.get("results") or []:
        decision = evaluate_replacement_candidate(
            result,
            expected_title=expected_title,
            expected_author=expected_author,
        )
        if decision.safe:
            safe_count += 1
        evaluated.append({
            **result,
            "bookguardSafe": decision.safe,
            "bookguardReason": decision.reason,
        })

    return {
        "bookId": book_id,
        "expectedTitle": expected_title,
        "expectedAuthor": expected_author,
        "safeCandidateCount": safe_count,
        "results": evaluated,
        "debug": search.get("debug"),
        "message": (
            "Safe replacement candidates are available."
            if safe_count
            else "No replacement candidate passed BookGuard's automatic safety gate."
        ),
    }


def _automatic_result(result_id: int) -> dict:
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    return item


@router.get("/results/{result_id}/wrong-content-preview")
def api_automatic_wrong_content_preview(result_id: int):
    """Read-only preflight for the WRONG_CONTENT remediation path."""
    item = _automatic_result(result_id)
    try:
        return {"resultId": result_id, **wrong_content_preview(item)}
    except AutomaticMaintenanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/results/{result_id}/remediate-wrong-content")
async def api_automatic_remediate_wrong_content(result_id: int, request: Request):
    """Quarantine + native detach + blocklist + safe replacement search.

    This endpoint deliberately does not auto-grab a replacement yet. v0.5.0
    requires the external-import staging gate to pass before a future automatic
    grab can be enabled, so downloaded bytes can be verified before admission to
    the managed library.
    """
    _require_mutation_mode()
    item = _automatic_result(result_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or payload.get("confirm") != "REMEDIATE_WRONG_CONTENT":
        raise HTTPException(
            status_code=400,
            detail="Explicit REMEDIATE_WRONG_CONTENT confirmation is required.",
        )
    try:
        return remediate_wrong_content(item)
    except AutomaticMaintenanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
