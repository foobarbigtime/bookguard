from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

from .action_paths import (
    EbookActionSafetyError,
    ebook_action_preview,
    resolve_writable_ebook_path,
)
from .bindery_client import BinderyClient, BinderyClientError, evaluate_replacement_candidate
from .config import settings
from .db import associations_inside_path, bindery_file_by_id
from .file_safety import allocate_unique_destination, roots_overlap, sha256_file
from .quarantine_fs import (
    QuarantineCommitError,
    QuarantineMoveError,
    commit_quarantine_or_rollback,
    move_to_quarantine,
)
from .scan_guard import CurrentScanError, require_current_scan_result
from .verifier import verification_for_result, verify_result


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


def _quarantine_destination(result: dict[str, Any], source: Path) -> Path:
    root = Path(settings.quarantine_root).resolve()
    media_roots = [Path(settings.ebook_root).resolve(), Path(settings.audiobook_root).resolve()]
    if any(roots_overlap(root, media) for media in media_roots):
        raise AutomaticMaintenanceError("Quarantine root must be outside configured media roots.")

    destination_dir = root / str(int(result["book_id"]))
    try:
        return allocate_unique_destination(destination_dir, source)
    except RuntimeError as exc:
        raise AutomaticMaintenanceError(
            "Unable to allocate a unique quarantine destination."
        ) from exc


def _require_current_scan_evidence(result: dict[str, Any]) -> None:
    """Translate the shared scan guard into automatic-maintenance errors."""
    try:
        require_current_scan_result(result)
    except CurrentScanError as exc:
        raise AutomaticMaintenanceError(str(exc)) from exc


def wrong_content_preview(result: dict[str, Any], client: BinderyClient | None = None) -> dict[str, Any]:
    client = client or BinderyClient()
    _require_current_scan_evidence(result)

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

    content_safe = all((
        verdict == "WRONG_CONTENT",
        confidence >= MIN_WRONG_CONTENT_CONFIDENCE,
        source.is_file(),
        exact_db_match,
        _tracked_path(book, str(result.get("stored_path") or "")),
        association_count == 1,
    ))
    try:
        action = ebook_action_preview(
            str(result.get("local_path") or ""),
            str(result.get("stored_path") or ""),
        )
    except EbookActionSafetyError as exc:
        raise AutomaticMaintenanceError(str(exc)) from exc
    safe = content_safe and action["ready"]

    return {
        "safe": safe,
        "contentSafe": content_safe,
        "verdict": verdict,
        "confidence": confidence,
        "bookId": int(result["book_id"]),
        "expectedTitle": str(book.get("title") or result.get("title") or ""),
        "expectedAuthor": _book_author(book, result),
        "sourceExists": source.is_file(),
        "exactDbMatch": exact_db_match,
        "binderyTracksPath": _tracked_path(book, str(result.get("stored_path") or "")),
        "associationCount": association_count,
        "ebookActionReady": action["ready"],
        "ebookActionChecks": action["checks"],
        "ebookActionBlockers": action["blockers"],
        "blocklistHistoryId": int(provenance["id"]) if provenance and provenance.get("id") else None,
        "blocklistSourceTitle": str(provenance.get("sourceTitle") or "") if provenance else "",
        "canBlocklistSource": provenance is not None,
        "storedPath": str(result.get("stored_path") or ""),
        "localPath": str(source),
    }


def unsafe_media_preview(
    result: dict[str, Any],
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Prove one current ebook result is safe for exact unsafe-media quarantine.

    This preview is intentionally narrower than generic triage quarantine. It
    requires a current deterministic-safety UNSAFE_FILE verification, stable
    source bytes, an exact unique Bindery association, and the separately
    configured writable ebook alias to still resolve to the same file.
    """
    client = client or BinderyClient()
    _require_current_scan_evidence(result)

    if str(result.get("format") or "").casefold() != "ebook":
        return {
            "safe": False,
            "reason": "Automatic unsafe-media quarantine currently supports ebook files only.",
        }

    verification = verification_for_result(result)
    evidence = (verification or {}).get("evidence") or {}
    security = evidence.get("security") if isinstance(evidence.get("security"), dict) else {}
    snapshot = (
        security.get("sourceSnapshot")
        if isinstance(security.get("sourceSnapshot"), dict)
        else {}
    )
    source = Path(str(result.get("local_path") or "")).resolve()
    expected_sha256 = str(snapshot.get("sha256") or "")

    deterministic_unsafe = all((
        str((verification or {}).get("verdict") or "") == "UNSAFE_FILE",
        int((verification or {}).get("confidence") or 0) == 100,
        str((verification or {}).get("source") or "") == "deterministic-safety",
        security.get("safe") is False,
        snapshot.get("sourceStable") is True,
        bool(expected_sha256),
    ))
    source_regular = source.is_file() and not source.is_symlink()
    source_hash_matches = bool(
        source_regular
        and expected_sha256
        and sha256_file(source) == expected_sha256
    )

    exact_db_row = bindery_file_by_id(int(result["file_id"])) if result.get("file_id") else None
    exact_db_match = bool(
        exact_db_row
        and int(exact_db_row["book_id"]) == int(result["book_id"])
        and str(exact_db_row["format"]) == str(result["format"])
        and str(exact_db_row["stored_path"]) == str(result["stored_path"])
    )
    association_count = len(associations_inside_path(str(result.get("stored_path") or "")))

    try:
        book = client.get_book(int(result["book_id"]))
    except BinderyClientError as exc:
        raise AutomaticMaintenanceError(str(exc)) from exc
    tracked = _tracked_path(book, str(result.get("stored_path") or ""))

    try:
        action = ebook_action_preview(
            str(result.get("local_path") or ""),
            str(result.get("stored_path") or ""),
        )
    except EbookActionSafetyError as exc:
        raise AutomaticMaintenanceError(str(exc)) from exc

    checks = {
        "deterministicUnsafeVerdict": deterministic_unsafe,
        "sourceRegularFile": source_regular,
        "sourceHashMatchesVerification": source_hash_matches,
        "exactBinderyAssociation": exact_db_match,
        "singleAssociation": association_count == 1,
        "binderyTracksPath": tracked,
        "writableAliasReady": bool(action["ready"]),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "safe": not blockers,
        "checks": checks,
        "blockers": blockers,
        "verificationId": (verification or {}).get("id"),
        "verificationSignature": str((verification or {}).get("signature") or ""),
        "verificationRevisionSource": str((verification or {}).get("source") or ""),
        "expectedSha256": expected_sha256,
        "associationCount": association_count,
        "storedPath": str(result.get("stored_path") or ""),
        "localPath": str(source),
        "writablePath": str(action.get("writablePath") or ""),
        "ebookActionChecks": action.get("checks") or {},
        "ebookActionBlockers": action.get("blockers") or [],
        "reason": (
            "Current deterministic unsafe-media evidence authorizes exact quarantine."
            if not blockers
            else "Unsafe-media quarantine boundary failed: " + ", ".join(blockers)
        ),
    }


def quarantine_unsafe_media(
    result: dict[str, Any],
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Quarantine one exact deterministically unsafe ebook and detach it.

    No replacement search, grab, admission, deletion, or unrelated association
    mutation is performed by this slice.
    """
    if not settings.allow_actions:
        raise AutomaticMaintenanceError("Automatic actions are disabled.")

    client = client or BinderyClient()
    _require_current_scan_evidence(result)

    verification = verify_result(result, force=True)
    if str(verification.get("verdict") or "") != "UNSAFE_FILE":
        raise AutomaticMaintenanceError(
            "Immediate deterministic verification no longer reports UNSAFE_FILE."
        )
    if int(verification.get("confidence") or 0) != 100:
        raise AutomaticMaintenanceError(
            "UNSAFE_FILE verification must have deterministic 100% confidence."
        )
    if str(verification.get("source") or "") != "deterministic-safety":
        raise AutomaticMaintenanceError(
            "Only deterministic file-safety/integrity evidence can authorize quarantine."
        )

    preview = unsafe_media_preview(result, client)
    if not preview.get("safe"):
        raise AutomaticMaintenanceError(
            preview.get("reason") or "Unsafe-media quarantine preflight failed."
        )

    source = Path(preview["localPath"]).resolve()
    expected_sha256 = str(preview["expectedSha256"])
    if sha256_file(source) != expected_sha256:
        raise AutomaticMaintenanceError(
            "Source bytes changed after deterministic unsafe verification."
        )

    try:
        action_source = resolve_writable_ebook_path(
            str(result.get("local_path") or ""),
            str(result.get("stored_path") or ""),
        )
    except EbookActionSafetyError as exc:
        raise AutomaticMaintenanceError(str(exc)) from exc

    if sha256_file(action_source) != expected_sha256:
        raise AutomaticMaintenanceError(
            "Writable ebook alias changed after verification; no file was moved."
        )

    destination = _quarantine_destination(result, source)

    try:
        move_to_quarantine(
            action_source,
            source,
            destination,
            expected_sha256=expected_sha256,
        )
    except QuarantineMoveError as exc:
        raise AutomaticMaintenanceError(
            f"Quarantine move failed before Bindery was changed: {exc}"
        ) from exc

    def commit_detach() -> None:
        client.deregister_file(
            int(result["book_id"]),
            str(result["stored_path"]),
        )
        for _ in range(20):
            if bindery_file_by_id(int(result["file_id"])) is None:
                return
            time.sleep(0.2)
        raise AutomaticMaintenanceError(
            "Bindery reported deregistration but the exact file association still exists."
        )

    try:
        commit_quarantine_or_rollback(
            commit_detach,
            action_source,
            source,
            destination,
            expected_sha256=expected_sha256,
        )
    except QuarantineCommitError as exc:
        rollback_error = (
            f" Rollback also failed: {exc.rollback_error}"
            if exc.rollback_error is not None
            else ""
        )
        raise AutomaticMaintenanceError(
            f"Bindery deregistration failed after quarantine: "
            f"{exc.cause}.{rollback_error}"
        ) from exc

    return {
        "ok": True,
        "outcome": "unsafe_media_quarantined",
        "bookId": int(result["book_id"]),
        "fileId": int(result["file_id"]),
        "storedPath": str(result["stored_path"]),
        "quarantinePath": str(destination),
        "sha256": expected_sha256,
        "binderyDetached": True,
        "permanentDeletion": False,
        "replacementRequested": False,
        "message": (
            "The exact deterministically unsafe media was quarantined and its "
            "matching Bindery association was detached. No replacement action ran."
        ),
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
    _require_current_scan_evidence(result)

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

    source_hash = sha256_file(source)
    destination = _quarantine_destination(result, source)

    try:
        action_source = resolve_writable_ebook_path(
            str(result.get("local_path") or ""),
            str(result.get("stored_path") or ""),
        )
    except EbookActionSafetyError as exc:
        raise AutomaticMaintenanceError(str(exc)) from exc
    if sha256_file(action_source) != source_hash:
        raise AutomaticMaintenanceError(
            "Writable ebook alias changed after verification; no file was moved."
        )

    try:
        move_to_quarantine(
            action_source,
            source,
            destination,
            expected_sha256=source_hash,
        )
    except QuarantineMoveError as exc:
        raise AutomaticMaintenanceError(
            f"Quarantine move failed before Bindery was changed: {exc}"
        ) from exc

    try:
        commit_quarantine_or_rollback(
            lambda: client.deregister_file(
                int(result["book_id"]),
                str(result["stored_path"]),
            ),
            action_source,
            source,
            destination,
            expected_sha256=source_hash,
        )
    except QuarantineCommitError as exc:
        rollback_error = (
            f" Rollback also failed: {exc.rollback_error}"
            if exc.rollback_error is not None
            else ""
        )
        raise AutomaticMaintenanceError(
            f"Bindery deregistration failed after quarantine: "
            f"{exc.cause}.{rollback_error}"
        ) from exc

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
