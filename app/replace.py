"""Replace: quarantine a bad file and let Bindery fetch a new copy, arr style.

BookGuard does the part only it can do (verify, then move the file out of the
library with Put back possible), and hands everything else to Bindery through
its API: blocklist the release the bad file came from, keep the book
monitored, and run Bindery's Automatic search, which picks, downloads and
imports a new copy with the user's own Bindery settings. The import watcher
then checks the new file like any other import.
"""

from __future__ import annotations

from .actions import ActionError, resolve_bindery_api_key
from .automatic import _history_items, _source_grab_event
from .bindery_client import BinderyClient, BinderyClientError
from .db import bindery_files_for_book, set_cleanup_followup
from .triage import triage_quarantine


def triage_replace(result: dict) -> tuple[int, str]:
    """Quarantine one Review/Reject file, then have Bindery replace it."""
    book_id = int(result["book_id"])
    try:
        others = [
            item for item in bindery_files_for_book(book_id, str(result["format"]))
            if item["stored_path"] != result["stored_path"]
        ]
    except Exception as exc:
        raise ActionError(f"Could not read Bindery's database, so nothing was moved: {exc}") from exc
    if others:
        raise ActionError(
            "Bindery already has another copy of this book, so there is nothing to replace. "
            "Use Quarantine to take this file out of the library."
        )
    client = BinderyClient(api_key=resolve_bindery_api_key(), timeout=30)
    try:
        grab = _source_grab_event(_history_items(client.list_history(book_id, limit=100)))
    except BinderyClientError as exc:
        raise ActionError(f"Could not read Bindery's history for this book, so nothing was moved: {exc}") from exc

    cleanup_id, _destination = triage_quarantine(result)

    problems = []
    if grab and grab.get("id"):
        try:
            client.blocklist_history(int(grab["id"]))
        except BinderyClientError as exc:
            problems.append(f"the old release could not be blocklisted ({exc})")
    else:
        problems.append("Bindery's history does not show which download this file came from, so nothing was blocklisted")
    try:
        client.set_book_monitored(book_id, True)
        answer = client.search_book_automatic(book_id)
        item = ((answer or {}).get("results") or {}).get(str(book_id)) or {}
        if not item.get("ok"):
            raise BinderyClientError(str(item.get("error") or item.get("code") or "no answer for this book"))
    except BinderyClientError as exc:
        message = (
            f"Needs you: the file is in quarantine, but Bindery's automatic search did not start ({exc}). "
            "In Bindery, open the book and click Automatic search, or pick a release by hand."
        )
        if problems:
            message += " Note: " + "; ".join(problems) + "."
        set_cleanup_followup(cleanup_id, message[:1000])
        return cleanup_id, message

    message = "Replacement requested: Bindery is searching for a new copy and will import it."
    if problems:
        message += " Note: " + "; ".join(problems) + "."
    set_cleanup_followup(cleanup_id, message[:1000])
    return cleanup_id, message
