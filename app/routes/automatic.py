from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..admission import (
    AdmissionSafetyError,
    admission_history,
    admission_readiness,
    admit_staged_ebook,
    reconcile_admission,
)
from ..automatic import AutomaticMaintenanceError, remediate_wrong_content, wrong_content_preview
from ..bindery_client import BinderyClient, BinderyClientError, evaluate_replacement_candidate
from ..db import result_by_id
from ..preimport import PreImportSafetyError, preimport_readiness
from ..staging import StagingSafetyError, list_staged_ebooks, verify_staged_ebook
from .models import ConfirmationRequest, require_confirmation


router = APIRouter(prefix="/api/automatic", tags=["automatic maintenance"])


class StagedVerificationRequest(BaseModel):
    relativePath: str = Field(min_length=1, max_length=4096)


class StagedAdmissionRequest(StagedVerificationRequest):
    confirm: str = Field(min_length=1, max_length=64)


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


@router.get("/admissions")
def api_automatic_admissions(limit: int = 100):
    """Return durable admission history and recovery state."""
    return admission_history(limit)


@router.post("/results/{result_id}/admit-staged-ebook")
def api_automatic_admit_staged_ebook(result_id: int, payload: StagedAdmissionRequest):
    """Verify and atomically publish one staged ebook to its former path."""
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
    """Confirm registration or request another Bindery library scan."""
    require_confirmation(payload, "RECONCILE_ADMISSION")
    try:
        return reconcile_admission(admission_id)
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
