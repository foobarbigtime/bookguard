from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
from typing import Any

from .bindery_client import BinderyClient, BinderyClientError, evaluate_replacement_candidate
from .config import settings
from .db import associations_inside_path, bindery_file_by_id, latest_scan
from .verifier_v048_final import verification_for_result, verify_result


class AutomaticMaintenanceError(RuntimeError):
    pass


MIN_WRONG_CONTENT_CONFIDENCE = 95


def _book_author(book: dict[str, Any], result: dict[str, Any]) -> str:
    author_obj = book.get("author") if isinstance(book.get("author"), dict) else {}
    return str(
        book.get("authorName")
        or author_obj.get("name")
        or author_obj.get("authorName")
        or result.get("author")
        or ""
    ).strip()


def _tracked_path(book: dict[str, Any], stored_path: str) -> bool:
    wanted = os.path.normpath(str(stored_path or ""))
    for entry in book.get("bookFiles") or []:
        if os.path.normpath(str(entry.get("path") or "")) == wanted:
            return True
    return False


def _history_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items") if isinstance(payload, dict) else None
    return [item for item in (items or []) if isinstance(item, dict)]


def _source_grab_event(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find a defensible grab event for the file we are remediating.

    We only blocklist when a bookImported event has the exact same source title
    as an earlier grabbed event. If that provenance pairing is absent, automatic
    mode refuses to guess which release created the bad file.
    """
    imports = [item for item in items if item.get("eventType") == "bookImported"]
    grabs = [item for item in items if item.get("eventType") == "grabbed"]
    for imported in imports:
        source = str(imported.get("sourceTitle") or "").strip()
        if not source:
            continue
        matches = [g for g in grabs if str(g.get("sourceTitle") or "").strip() == source]
        if matches:
            return max(matches, key=lambda item: int(item.get("id") or 0))
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quarantine_destination(result: dict[str, Any], source: Path) -> Path:
    root = Path(settings.quarantine_root).resolve()
    media_roots = [Path(settings.ebook_root).resolve(), Path(settings.audiobook_root).resolve()]
    if root in media_roots or any(root == media or media in root.parents for media in media_roots):
        raise AutomaticMaintenanceError("Quarantine root must be outside configured media roots.")

    destination_dir = root / str(int(result["book_id"]))
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if not destination.exists():
        return destination
    for index in range(2, 10000):
        candidate = destination_dir / f"{source.stem}-{index}{source.suffix}"
        if not candidate.exists():
            return candidate
    raise AutomaticMaintenanceError("Unable to allocate a unique quarantine destination.")


def _current_result_guard(result: dict[str, Any]) -> None:
    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise AutomaticMaintenanceError("A completed latest scan is required.")
    if result.get("scan_id") != scan.get("id"):
        raise AutomaticMaintenanceError("The result is not from the latest completed scan.")


def wrong_content_preview(result: dict[str, Any], client: BinderyClient | None = None) -> dict[str, Any]:
    client = client or BinderyClient()
    _current_result_guard(result)

    verification = verification_for_result(result)
    verdict = str((verification or {}).get("verdict") or "")
    confidence = int((verification or {}).get("confidence") or 0)
    source = Path(str(result.get("local_path") or ""))

    try:
        book = client.get_book(int(result["book_id"]))
        history = client.list_history(int(result["book_id"]), limit=100)
    except BinderyClientError as exc:
        raise AutomaticMaintenanceError(str(exc)) from exc

    provenance = _source_grab_event(_history_items(history))
    exact_db_row = bindery_file_by_id(int(result["file_id"])) if result.get("file_id") else None
    exact_db_match = bool(
        exact_db_row
        and int(exact_db_row["book_id"]) == int(result["book_id"])
        and str(exact_db_row["stored_path"]) == str(result["stored_path"])
    )
    association_count = len(associations_inside_path(str(result.get("stored_path") or "")))

    safe = all((
        verdict == "WRONG_CONTENT",
        confidence >= MIN_WRONG_CONTENT_CONFIDENCE,
        source.is_file(),
        exact_db_match,
        _tracked_path(book, str(result.get("stored_path") or "")),
        association_count == 1,
    ))

    return {
        "safe": safe,
        "verdict": verdict,
        "confidence": confidence,
        "bookId": int(result["book_id"]),
        "expectedTitle": str(book.get("title") or result.get("title") or ""),
        "expectedAuthor": _book_author(book, result),
        "sourceExists": source.is_file(),
        "exactDbMatch": exact_db_match,
        "binderyTracksPath": _tracked_path(book, str(result.get("stored_path") or "")),
        "associationCount": association_count,
        "blocklistHistoryId": int(provenance["id"]) if provenance and provenance.get("id") else None,
        "blocklistSourceTitle": str(provenance.get("sourceTitle") or "") if provenance else "",
        "canBlocklistSource": provenance is not None,
        "storedPath": str(result.get("stored_path") or ""),
        "localPath": str(source),
    }


def remediate_wrong_content(result: dict[str, Any], client: BinderyClient | None = None) -> dict[str, Any]:
    """Quarantine one independently verified wrong ebook and detach it natively.

    The file is moved before Bindery is deregistered. If deregistration fails,
    BookGuard attempts to move the file back to its original path. This avoids
    leaving a bad file inside Bindery's scan root after a successful detach.

    Reacquisition is SEARCH-ONLY in v0.5.0's first guarded slice. Release names
    are not sufficient proof of content identity, so candidates are surfaced
    but not automatically grabbed until a post-download/pre-import verification
    path exists.
    """
    if not settings.allow_actions:
        raise AutomaticMaintenanceError("Automatic actions are disabled.")

    client = client or BinderyClient()
    _current_result_guard(result)

    # Immediate content re-verification is mandatory at the mutation boundary.
    verification = verify_result(result, force=True)
    if str(verification.get("verdict") or "") != "WRONG_CONTENT":
        raise AutomaticMaintenanceError("Immediate re-verification no longer reports WRONG_CONTENT.")
    if int(verification.get("confidence") or 0) < MIN_WRONG_CONTENT_CONFIDENCE:
        raise AutomaticMaintenanceError("WRONG_CONTENT confidence is below the automatic safety threshold.")

    preview = wrong_content_preview(result, client)
    if not preview["safe"]:
        raise AutomaticMaintenanceError("Wrong-content safety preflight failed; manual review is required.")

    source = Path(preview["localPath"]).resolve()
    if str(result.get("format") or "") != "ebook" or not source.is_file():
        raise AutomaticMaintenanceError("Automatic wrong-content quarantine currently supports ebook files only.")

    source_hash = _sha256(source)
    destination = _quarantine_destination(result, source)

    try:
        shutil.move(str(source), str(destination))
    except Exception as exc:
        raise AutomaticMaintenanceError(f"Quarantine move failed before Bindery was changed: {exc}") from exc

    try:
        if not destination.is_file() or _sha256(destination) != source_hash:
            raise AutomaticMaintenanceError("Quarantined file checksum verification failed.")
        client.deregister_file(int(result["book_id"]), str(result["stored_path"]))
    except Exception as exc:
        rollback_error = ""
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and not source.exists():
                shutil.move(str(destination), str(source))
        except Exception as rollback_exc:
            rollback_error = f" Rollback also failed: {rollback_exc}"
        raise AutomaticMaintenanceError(f"Bindery deregistration failed after quarantine: {exc}.{rollback_error}") from exc

    blocklist = None
    blocklist_warning = ""
    if preview.get("blocklistHistoryId"):
        try:
            blocklist = client.blocklist_history(int(preview["blocklistHistoryId"]))
        except Exception as exc:
            blocklist_warning = f"Bad file is quarantined, but source release blocklisting failed: {exc}"
    else:
        blocklist_warning = "Bad file is quarantined, but no exact grab/import provenance pair was safe to blocklist."

    search = client.search_book(int(result["book_id"]))
    book = client.get_book(int(result["book_id"]))
    expected_title = str(book.get("title") or result.get("title") or "")
    expected_author = _book_author(book, result)

    evaluated: list[dict[str, Any]] = []
    safe_candidates = 0
    for candidate in search.get("results") or []:
        decision = evaluate_replacement_candidate(
            candidate,
            expected_title=expected_title,
            expected_author=expected_author,
        )
        if decision.safe:
            safe_candidates += 1
        evaluated.append({
            **candidate,
            "bookguardSafe": decision.safe,
            "bookguardReason": decision.reason,
        })

    return {
        "ok": True,
        "outcome": "quarantined_search_complete",
        "bookId": int(result["book_id"]),
        "quarantinePath": str(destination),
        "sha256": source_hash,
        "binderyStatus": str(book.get("status") or ""),
        "blocklist": blocklist,
        "warning": blocklist_warning,
        "safeCandidateCount": safe_candidates,
        "replacementCandidates": evaluated,
        "automaticGrab": False,
        "message": (
            "Wrong content quarantined and detached. Replacement search completed; "
            "automatic grabbing remains disabled until downloaded content can be verified before import."
        ),
    }
