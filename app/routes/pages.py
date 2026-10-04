from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..catalogue_move import list_moves
from ..config import settings
from ..diagnostics import _malware_report, diagnostics_snapshot
from ..health import health_checks, system_about
from ..scheduler import scheduled_tasks
from ..home import home_summary
from ..library_review import GROUPS, group_counts, open_review_items
from ..activity import RESULTS, WHAT, WHO, activity_events, activity_totals, filter_events
from ..history import operation_detail
from ..put_back import put_back_preview
from ..unmatched import VERDICTS, check_status, stored_checks
from ..duplicates import KINDS as DUPLICATE_KINDS, find_duplicates, fix_status
from ..db import (
    latest_counts,
    latest_reason_counts,
    latest_results,
    latest_scan,
    recent_cleanup_actions,
    recent_metadata_repairs,
)
from ..static_assets import static_url
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
templates.env.globals["static_url"] = static_url
ACTIVITY_PAGE_LIMIT = 500


@router.get("/", response_class=HTMLResponse)
def home_page(request: Request):
    # The scan dashboard used to live here and took filters in the query.
    if request.url.query:
        return _redirect_renamed(request, "/review/scan-results")
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={"home": home_summary(), "version": __version__},
    )


@router.get("/review/unmatched", response_class=HTMLResponse)
def unmatched_page(request: Request):
    items = stored_checks()
    order = ["JUNK", "BELONGS", "DUPLICATE", "OTHER_LANGUAGE", "NOT_IN_LIBRARY", "UNSURE"]
    groups = [(key, VERDICTS[key], [i for i in items if i["verdict"] == key]) for key in order]
    return templates.TemplateResponse(
        request=request,
        name="review_unmatched.html",
        context={
            "groups": groups,
            "total": len(items),
            "status": check_status(),
            "allow_actions": settings.allow_actions,
            "version": __version__,
        },
    )


@router.get("/review/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request):
    error = ""
    try:
        found = find_duplicates()
    except Exception as exc:  # Bindery's database unreadable: say so on the page
        found, error = [], str(exc)
    groups = [(key, label, [g for g in found if g["kind"] == key]) for key, label in DUPLICATE_KINDS.items()]
    return templates.TemplateResponse(
        request=request,
        name="review_duplicates.html",
        context={
            "groups": groups,
            "total": len(found),
            "fixable": sum(len(g["hideable"]) for g in found),
            "error": error,
            "status": fix_status(),
            "fix_duplicates": settings.fix_duplicates,
            "allow_actions": settings.allow_actions,
            "version": __version__,
        },
    )


@router.get("/review/scan-results", response_class=HTMLResponse)
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


@router.get("/activity", response_class=HTMLResponse)
def activity_page(
    request: Request,
    what: str = "",
    who: str = "",
    result: str = "",
    period: str = "",
    q: str = "",
):
    events = activity_events()
    shown = filter_events(events, what=what, who=who, result=result, period=period, query=q)
    return templates.TemplateResponse(
        request=request,
        name="activity.html",
        context={
            "events": shown[:ACTIVITY_PAGE_LIMIT],
            "total_shown": len(shown),
            "totals": activity_totals(shown),
            "filters": {"what": what, "who": who, "result": result, "period": period, "q": q},
            "filtering": any([what, who, result, period, q]),
            "what_options": WHAT,
            "who_options": WHO,
            "result_options": RESULTS,
            "version": __version__,
        },
    )


@router.get("/activity/{kind}/{record_id}", response_class=HTMLResponse)
def history_detail_page(request: Request, kind: str, record_id: int):
    detail = operation_detail(kind, record_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Operation record not found.")
    return templates.TemplateResponse(
        request=request,
        name="history_detail.html",
        context={
            "detail": detail,
            "put_back": put_back_preview(record_id, check_bytes=False) if kind == "cleanup" else None,
            "version": __version__,
        },
    )


@router.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    malware = _malware_report()
    return templates.TemplateResponse(
        request=request,
        name="system.html",
        context={
            "health": health_checks(malware),
            "tasks": scheduled_tasks(),
            "about": system_about(malware),
            "version": __version__,
        },
    )


@router.get("/system/advanced", response_class=HTMLResponse)
def diagnostics_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="diagnostics.html",
        context={
            "diagnostics": diagnostics_snapshot(),
            "version": __version__,
        },
    )


@router.get("/review/triage", response_class=HTMLResponse)
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
            "catalogue_moves": list_moves(50),
            "version": __version__,
        },
    )


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, group: str = "", language: str = ""):
    items = open_review_items()
    known = {key for key, _, _ in GROUPS}
    selected = [key for key in group.split(",") if key in known]
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "items": items,
            "groups": GROUPS,
            "tones": {key: tone for key, _, tone in GROUPS},
            "counts": group_counts(items),
            "group": ",".join(selected),
            "language_filter": language == "other",
            "non_english": sum(1 for item in items if item["language"]["nonEnglish"]),
            "allow_actions": settings.allow_actions,
            "repair_mode": settings.metadata_repair_mode,
            "catalogue_moves": list_moves(50),
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


# Pages renamed by the UI redesign. Old bookmarks keep working; the redirect is
# temporary (307) so a rollback to an older BookGuard is not stuck behind a
# browser-cached permanent redirect.
def _redirect_renamed(request: Request, target: str) -> RedirectResponse:
    query = request.url.query
    return RedirectResponse(f"{target}?{query}" if query else target, status_code=307)


def _add_renamed_page(old: str, new: str) -> None:
    def redirect(request: Request):
        return _redirect_renamed(request, new)

    router.add_api_route(old, redirect, methods=["GET"], include_in_schema=False)


for _old, _new in {
    "/triage": "/review/triage",
    "/attention": "/",
    "/history": "/activity",
    "/diagnostics": "/system",
}.items():
    _add_renamed_page(_old, _new)


@router.get("/history/{kind}/{record_id}", include_in_schema=False)
def renamed_history_detail(request: Request, kind: str, record_id: int):
    return _redirect_renamed(request, f"/activity/{kind}/{record_id}")
