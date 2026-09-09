from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .actions import ActionError, detach, quarantine
from .config import settings
from .db import init_local_db, latest_counts, latest_results, latest_scan, result_by_id
from .scanner import start_scan


app = FastAPI(title="BookGuard", version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup() -> None:
    init_local_db()
    if settings.scan_on_start:
        start_scan()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, classification: str | None = None):
    scan = latest_scan()
    counts = latest_counts()
    results = latest_results(classification=classification, limit=1000)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "scan": scan,
            "counts": counts,
            "results": results,
            "classification": classification or "",
            "allow_actions": settings.allow_actions,
            "bindery_db_exists": os.path.exists(settings.bindery_db),
            "sample_files": settings.sample_files,
        },
    )


@app.post("/api/scan")
def api_scan():
    try:
        scan_id = start_scan()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not scan_id:
        raise HTTPException(status_code=409, detail="A scan is already running.")
    return {"scan_id": scan_id}


@app.get("/api/status")
def api_status():
    return {"scan": latest_scan(), "counts": latest_counts()}


@app.post("/api/results/{result_id}/detach")
def api_detach(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    if item["classification"] == "PASS":
        raise HTTPException(status_code=400, detail="Refusing to detach a PASS result.")
    try:
        detach(item["book_id"], item["stored_path"])
    except ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/api/results/{result_id}/quarantine")
def api_quarantine(result_id: int):
    item = result_by_id(result_id)
    if not item:
        raise HTTPException(status_code=404, detail="Result not found.")
    if item["classification"] == "PASS":
        raise HTTPException(status_code=400, detail="Refusing to quarantine a PASS result.")
    try:
        destination = quarantine(item["book_id"], item["stored_path"], item["local_path"])
    except ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "destination": destination}
