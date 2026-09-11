from __future__ import annotations

from fastapi import HTTPException

from .bindery_client import BinderyClient, BinderyClientError, evaluate_replacement_candidate
from .main_v048 import app


app.version = "0.5.0"


@app.get("/api/automatic/bindery-status")
def api_automatic_bindery_status():
    """Read-only connectivity check for the Automatic Maintenance pipeline."""
    try:
        status = BinderyClient().system_status()
    except BinderyClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"ok": True, "bindery": status}


@app.get("/api/automatic/books/{book_id}/replacement-preview")
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
