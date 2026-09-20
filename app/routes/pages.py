from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..attention import attention_snapshot
from ..config import settings
from ..diagnostics import diagnostics_snapshot
from ..history import operation_history
from ..db import (
    latest_counts,
    latest_reason_counts,
    latest_results,
    latest_scan,
    recent_cleanup_actions,
    recent_metadata_repairs,
)
from ..services.dashboard import (
    compact_result,
    enrich_results,
    latest_missing_cleanup,
    latest_repair_candidates,
    scan_timing,
)
from ..triage import TRIAGE_CLASSES, triage_state, triage_summary


router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    classification: str | None = None,
    reason_code: str | None = None,
):
    scan = latest_scan()
    counts = latest_counts()
    results = latest_results(
        classification=classification,
        reason_code=reason_code,
        limit=settings.dashboard_result_limit,
    )
    missing_cleanup = latest_missing_cleanup() if classification == "MISSING" else None
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "scan": scan,
            "timing": scan_timing(scan),
            "counts": counts,
            "review_reasons": latest_reason_counts("REVIEW"),
            "results": enrich_results(results),
            "classification": classification or "",
            "reason_code": reason_code or "",
            "allow_actions": settings.allow_actions,
            "repair_mode": settings.metadata_repair_mode,
            "bindery_db_exists": os.path.exists(settings.bindery_db),
            "sample_files": settings.sample_files,
            "poll_ms": settings.dashboard_poll_ms,
            "missing_cleanup": missing_cleanup,
            "cleanup_history": recent_cleanup_actions(50) if classification == "MISSING" else [],
            "version": __version__,
        },
    )


@router.get("/attention", response_class=HTMLResponse)
def attention_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="attention.html",
        context={
            "attention": attention_snapshot(),
            "version": __version__,
        },
    )



@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "history": operation_history(),
            "version": __version__,
        },
    )


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="diagnostics.html",
        context={
            "diagnostics": diagnostics_snapshot(),
            "version": __version__,
        },
    )


@router.get("/triage", response_class=HTMLResponse)
def triage_page(
    request: Request,
    classification: str = "REVIEW",
    reason_code: str | None = None,
    show_resolved: int = 0,
):
    classification = str(classification or "REVIEW").upper()
    if classification not in TRIAGE_CLASSES:
        classification = "REVIEW"

    rows = latest_results(
        classification=classification,
        reason_code=reason_code,
        limit=5000,
    )
    enriched = []
    for row in rows:
        state = triage_state(row)
        if not show_resolved and state["resolved"]:
            continue
        enriched.append({**row, **compact_result(row), "triage": state})

    return templates.TemplateResponse(
        request=request,
        name="triage.html",
        context={
            "classification": classification,
            "reason_code": reason_code or "",
            "show_resolved": bool(show_resolved),
            "summary": triage_summary(),
            "reasons": latest_reason_counts(classification),
            "rows": enriched,
            "allow_actions": settings.allow_actions,
            "cleanup_history": recent_cleanup_actions(100),
            "version": __version__,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    values = settings.public_dict()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "settings": values,
            "config_dir": settings.config_dir,
            "bindery_db_exists": os.path.exists(settings.bindery_db),
            "audiobook_root_exists": os.path.exists(settings.audiobook_root),
            "ebook_root_exists": os.path.exists(settings.ebook_root),
            "quarantine_root_exists": os.path.exists(settings.quarantine_root),
            "scan": latest_scan(),
            "version": __version__,
        },
    )


@router.get("/repairs", response_class=HTMLResponse)
def repairs_page(request: Request):
    candidates = latest_repair_candidates()
    return templates.TemplateResponse(
        request=request,
        name="repairs.html",
        context={
            "candidates": candidates,
            "candidate_count": len(candidates),
            "repairs": recent_metadata_repairs(200),
            "repair_mode": settings.metadata_repair_mode,
            "version": __version__,
        },
    )
