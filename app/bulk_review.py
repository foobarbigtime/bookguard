"""Read-only plan for applying one Review group's fix to many books at once.

Nothing here changes a file or Bindery. For each open book in a group that has
a safe one-step fix (move to the correct book, fix the details, replace) it
runs the same preflight the single-book action runs and reports which books
could be fixed right now and why the others could not. The browser then applies
the eligible books one at a time through the existing guarded endpoints, so
every book is re-proven, confirmed, and recorded exactly as if it had been done
by hand.
"""

from __future__ import annotations

from typing import Any, Callable

from .actions import ActionError, resolve_bindery_api_key
from .bindery_client import BinderyClient
from .catalogue_move import move_preview
from .config import settings
from .db import associations_inside_path, bindery_files_for_book, result_by_id
from .library_review import latest_verifications, open_review_items
from .triage import triage_action_preview
from .verifier import verified_repair_preview

# group -> (single-book endpoint template, confirmation word, button label)
BULK_ACTIONS: dict[str, dict[str, str]] = {
    "move": {
        "endpoint": "/api/triage/{id}/move-to-correct-book",
        "confirm": "MOVE_TO_CORRECT_BOOK",
        "label": "Move all to their correct books",
    },
    "metadata": {
        "endpoint": "/api/verification/{id}/repair",
        "confirm": "REPAIR",
        "label": "Fix all details",
    },
    "wrong": {
        "endpoint": "/api/triage/{id}/replace",
        "confirm": "REPLACE",
        "label": "Replace all",
    },
}


def _move(row: dict, verification: dict, client: BinderyClient) -> tuple[bool, str, str]:
    preview = move_preview(row, verification, client)
    detail = f"Move to “{preview['targetTitle']}”." if preview["eligible"] else ""
    return preview["eligible"], ", ".join(preview["blockers"]), detail


def _metadata(row: dict, verification: dict, client: BinderyClient) -> tuple[bool, str, str]:
    if settings.metadata_repair_mode != "safe":
        return False, "metadataRepairNotSafeMode", ""
    preview = verified_repair_preview(row, verification)
    if not (preview.get("eligible") and preview.get("safe")):
        return False, str(preview.get("reason") or "notSafe"), ""
    before, after = preview["before"], preview["after"]
    detail = (f"{before.get('title') or '—'} / {before.get('author') or '—'} → "
              f"{after.get('title') or '—'} / {after.get('author') or '—'}")
    return True, "", detail


def _wrong(row: dict, verification: dict, client: BinderyClient) -> tuple[bool, str, str]:
    if not settings.allow_actions:
        return False, "binderyActionsDisabled", ""
    try:
        others = [f for f in bindery_files_for_book(int(row["book_id"]), str(row["format"]))
                  if f["stored_path"] != row["stored_path"]]
    except Exception:
        return False, "binderyDatabaseUnreadable", ""
    if others:
        return False, "binderyHasAnotherCopy", ""
    try:
        inside = associations_inside_path(str(row["stored_path"]))
    except Exception:
        return False, "binderyDatabaseUnreadable", ""
    if len(inside) != 1:  # the same guard quarantine applies: never take another book's folder along
        return False, "binderyTracksOtherBooksInside", ""
    try:
        preview = triage_action_preview(row, "quarantine")
    except ActionError as exc:
        return False, str(exc), ""
    if not preview.get("safe"):
        return False, str(preview.get("reason") or "notSafe"), ""
    return True, "", "Quarantine the file, blocklist its release, and search for a new copy."


_CHECKS: dict[str, Callable[[dict, dict, BinderyClient], tuple[bool, str, str]]] = {
    "move": _move,
    "metadata": _metadata,
    "wrong": _wrong,
}


def bulk_preview(group: str, client: BinderyClient | None = None) -> dict[str, Any]:
    """Which open books in ``group`` can be fixed now, and why the rest cannot."""
    if group not in BULK_ACTIONS:
        raise ValueError("Bulk fixes are available for: " + ", ".join(sorted(BULK_ACTIONS)) + ".")
    # A mixed audiobook folder is never moved or re-labelled as a whole, but a "wrong book"
    # verdict on it can be Replaced: the folder is quarantined (Put back restores it) and
    # Bindery searches for a clean copy.
    items = [item for item in open_review_items()
             if item["group"] == group and (group == "wrong" or not item["mixedAudio"])]
    verifications = latest_verifications([item["id"] for item in items])
    if client is None and items and group == "move":
        client = BinderyClient(api_key=resolve_bindery_api_key())
    check = _CHECKS[group]
    books: list[dict[str, Any]] = []
    for item in items:
        row = result_by_id(item["id"])
        verification = verifications.get(item["id"])
        if not row or not verification:
            ok, why, detail = False, "notVerified", ""
        else:
            try:
                ok, why, detail = check(row, verification, client)
            except Exception as exc:  # one bad book must not hide the others
                ok, why, detail = False, f"previewFailed: {exc}"[:200], ""
        if ok and group == "wrong" and item["mixedAudio"]:
            detail = ("The whole folder (it holds other books too) goes to quarantine, "
                      "then Bindery searches for a clean copy.")
        books.append({"id": item["id"], "title": item["title"], "author": item["author"],
                      "eligible": ok, "blockers": why, "detail": detail})
    action = BULK_ACTIONS[group]
    return {
        "group": group,
        "label": action["label"],
        "endpoint": action["endpoint"],
        "confirm": action["confirm"],
        "eligible": sum(1 for book in books if book["eligible"]),
        "total": len(books),
        "books": books,
    }
