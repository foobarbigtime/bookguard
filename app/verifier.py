from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .audiobook_evidence import build_audiobook_evidence
from .catalogue_relationship import classify_relationship, latest_ebook_scan_status
from .config import settings
from .db import (
    create_metadata_repair,
    finish_metadata_repair,
    latest_results,
    latest_review_results,
    latest_scan,
    local_conn,
)
from .ebook_extraction import extract_ebook_identity
from .ebook_security import inspect_ebook_security
from .isbn_evidence import isbn_evidence
from .file_snapshot import (
    SnapshotError,
    assert_snapshot_source_current,
    stable_file_fingerprint,
    verification_snapshot,
)
from .malware_scan import probe_clamd
from .matcher import normalize
from .library_check import serialized_start, start_allowed
from .media_discovery import resolve_ebook_target
from .media_evidence import detect_file_media_kind, inspect_media_path, media_set_fingerprint
from .metadata import ebook_metadata
from .repair import (
    RepairError,
    apply_repair_changes,
    require_current_scan_result,
    verify_repair_changes,
)
from .series_titles import bindery_series_context
from .triage import result_signature, triage_state, triage_states
from .tika_client import test_connection
from .verification_status import malware_scan_inconclusive, verification_is_inconclusive
from .verification_engine import classify_identity


VERIFIER_VERSION = "28"
VERDICTS = {
    "VERIFIED_CORRECT",
    "METADATA_ERROR",
    "WRONG_CONTENT",
    "INSUFFICIENT_EVIDENCE",
    "UNSAFE_FILE",
    "WRONG_MEDIA_TYPE",
}

_job_lock = threading.Lock()
_job_state: dict = {
    "status": "idle",
    "job_id": None,
    "classification": None,
    "reason_code": None,
    "total": 0,
    "processed": 0,
    "counts": {},
    "current": "",
    "error": None,
    "cacheHits": 0,
    "postponed": 0,
    "postponedReason": "",
    "elapsedSeconds": 0,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_tika() -> dict:
    """Expose the Tika connectivity check through the verifier service API."""
    return test_connection()


def test_malware_scanner() -> dict:
    """Probe the deployment-configured ClamAV daemon without sending media bytes."""
    return probe_clamd(
        host=settings.verification_clamd_host,
        port=settings.verification_clamd_port,
        timeout_seconds=min(10, settings.verification_malware_timeout_seconds),
    )


def init_verification_db() -> None:
    with local_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS content_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL UNIQUE,
                result_id INTEGER NOT NULL,
                scan_id TEXT NOT NULL,
                file_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                format TEXT NOT NULL,
                author TEXT NOT NULL,
                title TEXT NOT NULL,
                target_path TEXT NOT NULL,
                file_fingerprint TEXT NOT NULL,
                verdict TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                source TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_content_verifications_updated
                ON content_verifications(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_content_verifications_result
                ON content_verifications(result_id);
            CREATE INDEX IF NOT EXISTS idx_content_verifications_verdict
                ON content_verifications(verdict);
            """
        )
        conn.commit()


def _file_fingerprint(path: str) -> str:
    try:
        return stable_file_fingerprint(
            path,
            max_bytes=settings.verification_snapshot_max_bytes,
        )
    except SnapshotError as exc:
        return f"unsafe:{exc.code}"


class CatalogueUnavailable(RuntimeError):
    """Identity policy cannot be established while the catalogue is unavailable."""


def _verification_context(result: dict) -> dict:
    if "_verification_series" in result:
        return result
    names = bindery_series_context(result.get("book_id")) if result.get("format") == "ebook" else []
    if names is None:
        raise CatalogueUnavailable("Bindery series data is unavailable; verification is postponed. Try again when the catalogue is readable.")
    return {**result, "_verification_series": names}


def _verification_signature(result: dict, target_path: str, fingerprint: str) -> str:
    result = _verification_context(result)
    policy = json.dumps(
        {
            "enabled": settings.verification_enabled,
            "fileSignatures": settings.verification_file_signatures,
            "archiveSafety": settings.verification_archive_safety,
            "epubStructure": settings.verification_epub_structure,
            "pdfIntegrity": settings.verification_pdf_integrity,
            "malwareScan": settings.verification_malware_scan,
            "clamdHost": settings.verification_clamd_host,
            "clamdPort": settings.verification_clamd_port,
            "malwareMaxBytes": settings.verification_malware_max_bytes,
            "snapshotMaxBytes": settings.verification_snapshot_max_bytes,
            "maxTextChars": settings.verification_max_text_chars,
            "pdfPages": settings.verification_pdf_pages,
            "useTika": settings.verification_use_tika,
            "tikaUrl": settings.verification_tika_url,
            "identity": {
                "titleMinSharedWords": settings.title_min_shared_words,
                "allowAuthorSurnameMatch": settings.allow_author_surname_match,
                "authorAliases": settings.author_aliases,
                "musicGenres": settings.music_genres,
                "rejectMusicMismatch": settings.reject_music_mismatch,
                "rejectStrongMismatch": settings.reject_strong_mismatch,
                "strongMismatchMinSamples": settings.strong_mismatch_min_samples,
                "strongMismatchConsensusPercent": settings.strong_mismatch_consensus_percent,
                "seriesNames": result["_verification_series"],
            },
        },
        sort_keys=True,
    )
    raw = "|".join(
        [VERIFIER_VERSION, result_signature(result), target_path, fingerprint, policy]
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _decode_row(row) -> dict:
    item = dict(row)
    item["evidence"] = json.loads(item.pop("evidence_json"))
    item["cached"] = True
    return item


def _verification_for_fingerprint(
    result: dict,
    target: str,
    fingerprint: str,
) -> dict | None:
    init_verification_db()
    signature = _verification_signature(result, target, fingerprint)
    with local_conn() as conn:
        row = conn.execute(
            """SELECT * FROM content_verifications
               WHERE signature=? OR signature GLOB ?
               ORDER BY updated_at DESC, id DESC LIMIT 1""",
            (signature, signature + ":*"),
        ).fetchone()
    if not row:
        return None
    item = _decode_row(row)
    # A scanner outage is not a finding about the file: never reuse it, so the
    # next verification scans again instead of repeating the outage.
    if verification_is_inconclusive(item.get("verdict"), item.get("evidence")):
        return None
    return item


def _ebook_directory_media_mismatch(result: dict) -> dict | None:
    raw_path = str(result.get("local_path") or "")
    target = Path(raw_path)
    if not target.is_dir():
        return None
    inventory = inspect_media_path(raw_path)
    counts = dict(inventory.get("counts") or {})
    if int(counts.get("audiobook") or 0) < 1 or int(counts.get("ebook") or 0) > 0:
        return None
    return inventory


def verification_for_result(result: dict) -> dict | None:
    try:
        result = _verification_context(result)
    except CatalogueUnavailable:
        return None
    if result.get("format") == "audiobook":
        target = str(result.get("local_path") or "")
        fingerprint = media_set_fingerprint(target)
    else:
        mismatch = _ebook_directory_media_mismatch(result)
        if mismatch is not None:
            target = str(result.get("local_path") or "")
            fingerprint = media_set_fingerprint(target)
        else:
            target = resolve_ebook_target(result.get("local_path") or "")
            fingerprint = _file_fingerprint(target)
    return _verification_for_fingerprint(result, target, fingerprint)


def _save_verification(
    result: dict,
    target_path: str,
    fingerprint: str,
    verdict: str,
    confidence: int,
    source: str,
    evidence: dict,
) -> dict:
    init_verification_db()
    # The prefix identifies reusable evidence; the suffix owns one result's
    # receipt. Reusing proof must not move an older scan's record to a new scan.
    signature = _verification_signature(result, target_path, fingerprint) + f":{int(result['id'])}"
    now = _utc_now()
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO content_verifications(
                signature, result_id, scan_id, file_id, book_id, format,
                author, title, target_path, file_fingerprint, verdict,
                confidence, source, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signature) DO UPDATE SET
                result_id=excluded.result_id,
                scan_id=excluded.scan_id,
                verdict=excluded.verdict,
                confidence=excluded.confidence,
                source=excluded.source,
                evidence_json=excluded.evidence_json,
                updated_at=excluded.updated_at
            """,
            (
                signature,
                result["id"],
                result["scan_id"],
                result["file_id"],
                result["book_id"],
                result["format"],
                result["author"],
                result["title"],
                target_path,
                fingerprint,
                verdict,
                int(max(0, min(100, confidence))),
                source,
                json.dumps(evidence, ensure_ascii=False),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_verifications WHERE signature=? LIMIT 1",
            (signature,),
        ).fetchone()
        conn.commit()
    item = _decode_row(row)
    item["cached"] = False
    return item


def _record_cached_verification(result: dict, cached: dict) -> dict:
    """Link unchanged evidence to this result while retaining historical receipts."""
    if cached["result_id"] == result["id"] and cached["scan_id"] == result["scan_id"]:
        return cached
    evidence = dict(cached["evidence"])
    evidence["cacheReuse"] = _original_verification(cached)
    current = _save_verification(
        result, cached["target_path"], cached["file_fingerprint"],
        cached["verdict"], cached["confidence"], cached["source"], evidence,
    )
    current["cached"] = True
    return current


def _original_verification(cached: dict) -> dict:
    """Carry original proof provenance, including legacy receipt chains."""
    original = cached
    visited = set()
    while original["id"] not in visited:
        visited.add(original["id"])
        reuse = original["evidence"].get("cacheReuse") or {}
        if reuse.get("verifiedAt"):
            return {key: reuse[key] for key in ("verificationId", "scanId", "verifiedAt")}
        if not reuse.get("verificationId"):
            break
        with local_conn() as conn:
            row = conn.execute("SELECT * FROM content_verifications WHERE id=?", (reuse["verificationId"],)).fetchone()
        if row is None:
            break
        original = _decode_row(row)
    return {"verificationId": original["id"], "scanId": original["scan_id"],
            "verifiedAt": original["updated_at"] or original["created_at"]}


def _snapshot_failure_result(
    result: dict,
    target: str,
    exc: SnapshotError,
) -> dict:
    unsafe_codes = {
        "symlink",
        "non_regular",
        "changed",
        "no_nofollow_support",
        "snapshot_root",
        "snapshot_write",
    }
    unsafe = exc.code in unsafe_codes
    verdict = "UNSAFE_FILE" if unsafe else "INSUFFICIENT_EVIDENCE"
    confidence = 100 if unsafe else 0
    fingerprint = f"unsafe:{exc.code}"
    security = {
        "safe": False,
        "message": str(exc),
        "failures": ["sourceStability"] if unsafe else [],
        "checks": {
            "sourceStability": {
                "status": "failed",
                "code": exc.code,
                "message": str(exc),
            }
        },
    }
    evidence = {
        "expected": {
            "title": result.get("title", ""),
            "author": result.get("author", ""),
        },
        "embedded": {},
        "content": {},
        "metadata_matches_expected": False,
        "security": security,
        "notes": [str(exc)],
        "explanation": (
            "Book identity was not accepted because the tracked source could not "
            "be held stable as one regular file."
            if unsafe
            else "Book identity could not be evaluated because no stable readable source snapshot was available."
        ),
    }
    return _save_verification(
        result,
        target,
        fingerprint,
        verdict,
        confidence,
        "source-snapshot",
        evidence,
    )


def verify_result(result: dict, force: bool = False) -> dict:
    if not settings.verification_enabled:
        raise RuntimeError("Content verification is disabled in Settings.")
    result = _verification_context(result)

    if result.get("format") == "audiobook":
        target = str(result.get("local_path") or "")
        fingerprint = media_set_fingerprint(target)
        if not force:
            cached = _verification_for_fingerprint(result, target, fingerprint)
            if cached:
                return _record_cached_verification(result, cached)
        audiobook = build_audiobook_evidence(result, target)
        return _save_verification(
            result,
            target,
            fingerprint,
            str(audiobook["verdict"]),
            int(audiobook["confidence"]),
            str(audiobook["source"]),
            dict(audiobook["evidence"]),
        )

    if result.get("format") != "ebook":
        raise RuntimeError(
            f"Unsupported media format for content verification: {result.get('format')!r}"
        )

    directory_mismatch = _ebook_directory_media_mismatch(result)
    if directory_mismatch is not None:
        target = str(result.get("local_path") or "")
        fingerprint = media_set_fingerprint(target)
        if not force:
            cached = _verification_for_fingerprint(result, target, fingerprint)
            if cached:
                return _record_cached_verification(result, cached)
        evidence = {
            "expected": {
                "title": result.get("title", ""),
                "author": result.get("author", ""),
                "mediaKind": "ebook",
            },
            "actualMedia": directory_mismatch,
            "metadata_matches_expected": False,
            "notes": [],
            "reasonCode": "EXPECTED_EBOOK_FOUND_AUDIO",
            "explanation": (
                "Bindery expects an ebook, but the tracked directory contains "
                "readable audio media and no deterministically identified ebook."
            ),
        }
        return _save_verification(
            result,
            target,
            fingerprint,
            "WRONG_MEDIA_TYPE",
            100,
            "media-kind",
            evidence,
        )

    target = resolve_ebook_target(result.get("local_path") or "")
    started = time.monotonic()
    if not force:
        # Hash the file read-only and consult the cache before copying it. An
        # unchanged book then costs one read instead of a full private copy and
        # fsync. The hash uses the same no-follow descriptor and stability checks
        # as the snapshot; failures fall through to the snapshot path unchanged.
        precheck = _file_fingerprint(target)
        if precheck.startswith("sha256:"):
            cached = _verification_for_fingerprint(result, target, precheck)
            if cached:
                return _record_cached_verification(result, cached)
    snapshot_root = Path(settings.config_dir) / ".verification-snapshots"
    try:
        with verification_snapshot(
            target,
            temp_root=snapshot_root,
            max_bytes=settings.verification_snapshot_max_bytes,
        ) as snapshot:
            fingerprint = snapshot.fingerprint
            snapshot_done = time.monotonic()
            if not force:
                cached = _verification_for_fingerprint(result, target, fingerprint)
                if cached:
                    assert_snapshot_source_current(snapshot)
                    return _record_cached_verification(result, cached)

            media_kind = detect_file_media_kind(str(snapshot.path))
            if media_kind.get("kind") == "audiobook":
                assert_snapshot_source_current(snapshot)
                evidence = {
                    "expected": {
                        "title": result.get("title", ""),
                        "author": result.get("author", ""),
                        "mediaKind": "ebook",
                    },
                    "actualMedia": media_kind,
                    "metadata_matches_expected": False,
                    "notes": [],
                    "reasonCode": "EXPECTED_EBOOK_FOUND_AUDIO",
                    "explanation": (
                        "Bindery expects an ebook, but the stable tracked bytes contain "
                        "a readable audio stream."
                    ),
                }
                return _save_verification(
                    result,
                    target,
                    fingerprint,
                    "WRONG_MEDIA_TYPE",
                    100,
                    "media-kind",
                    evidence,
                )

            security = inspect_ebook_security(
                snapshot.path,
                check_file_signatures=settings.verification_file_signatures,
                check_archive_safety=settings.verification_archive_safety,
                check_epub_structure=settings.verification_epub_structure,
                check_pdf_integrity=settings.verification_pdf_integrity,
                check_malware=settings.verification_malware_scan,
                clamd_host=settings.verification_clamd_host,
                clamd_port=settings.verification_clamd_port,
                malware_timeout_seconds=settings.verification_malware_timeout_seconds,
                malware_max_bytes=settings.verification_malware_max_bytes,
            )
            security["sourceSnapshot"] = {
                "sha256": snapshot.sha256,
                "size": snapshot.size,
                "sourceStable": True,
            }
            security_done = time.monotonic()
            if not security["safe"] and malware_scan_inconclusive(security):
                assert_snapshot_source_current(snapshot)
                malware = (security.get("checks") or {}).get("malwareScan") or {}
                evidence = {
                    "expected": {
                        "title": result.get("title", ""),
                        "author": result.get("author", ""),
                    },
                    "embedded": {},
                    "content": {},
                    "metadata_matches_expected": False,
                    "security": security,
                    "malwareScanInconclusive": True,
                    "notes": [str(malware.get("message") or security["message"])],
                    "explanation": (
                        "The malware scan could not complete, so neither file safety nor "
                        "book identity was established. This is not evidence that the file "
                        "is unsafe; it will be scanned again on the next verification."
                    ),
                }
                return _save_verification(
                    result,
                    target,
                    fingerprint,
                    "INSUFFICIENT_EVIDENCE",
                    0,
                    "malware-scan-inconclusive",
                    evidence,
                )
            if not security["safe"]:
                assert_snapshot_source_current(snapshot)
                evidence = {
                    "expected": {
                        "title": result.get("title", ""),
                        "author": result.get("author", ""),
                    },
                    "embedded": {},
                    "content": {},
                    "metadata_matches_expected": False,
                    "security": security,
                    "notes": [security["message"]],
                    "explanation": (
                        "Book identity was not evaluated because deterministic file safety "
                        "or integrity validation failed."
                    ),
                }
                return _save_verification(
                    result,
                    target,
                    fingerprint,
                    "UNSAFE_FILE",
                    100,
                    "deterministic-safety",
                    evidence,
                )

            extracted = extract_ebook_identity(str(snapshot.path))
            verdict, confidence, evidence = classify_identity(
                {
                    **result,
                    "series": result["_verification_series"],
                    "isbn_evidence": isbn_evidence(result.get("book_id"), extracted.identifiers),
                },
                extracted.metadata,
                extracted.text,
                extracted.identifiers,
                extracted.notes,
                extracted.front_text,
            )
            assert_snapshot_source_current(snapshot)
            evidence["security"] = security
            identity_done = time.monotonic()
            evidence["timings"] = {
                "snapshotMs": round((snapshot_done - started) * 1000),
                "safetyChecksMs": round((security_done - snapshot_done) * 1000),
                "identityMs": round((identity_done - security_done) * 1000),
            }
            if evidence.get("actualBook"):
                # Read-only: how this file relates to the book it really is.
                catalogue = classify_relationship(
                    snapshot.sha256, evidence["actualBook"], latest_ebook_scan_status,
                )
                evidence["catalogue"] = catalogue
                evidence["explanation"] = f"{evidence['explanation']} {catalogue['explanation']}"
            return _save_verification(
                result,
                target,
                fingerprint,
                verdict,
                confidence,
                extracted.source,
                evidence,
            )
    except SnapshotError as exc:
        return _snapshot_failure_result(result, target, exc)


def verified_repair_preview(result: dict, verification: dict | None = None) -> dict:
    verification = verification or verification_for_result(result)
    if not verification:
        return {
            "eligible": False,
            "safe": False,
            "kind": "EPUB_METADATA",
            "reason": "Run content verification first.",
            "before": {},
            "after": {},
        }

    verdict = str(verification.get("verdict") or "")
    confidence = int(verification.get("confidence") or 0)
    if verdict != "METADATA_ERROR" or confidence < 90:
        return {
            "eligible": False,
            "safe": False,
            "kind": "EPUB_METADATA",
            "reason": "Only high-confidence METADATA_ERROR verdicts can use verified metadata repair.",
            "before": {},
            "after": {},
        }

    target = resolve_ebook_target(result.get("local_path") or "")
    if Path(target).suffix.lower() != ".epub":
        return {
            "eligible": False,
            "safe": False,
            "kind": "EPUB_METADATA",
            "reason": "Verified metadata writing is currently limited to EPUB files.",
            "before": {},
            "after": {},
        }

    current = ebook_metadata(target)
    if current.get("error"):
        return {
            "eligible": False,
            "safe": False,
            "kind": "EPUB_METADATA",
            "reason": "The current EPUB metadata could not be read safely.",
            "before": {},
            "after": {},
        }

    before = {
        "path": target,
        "title": str(current.get("title") or ""),
        "author": str(current.get("author") or ""),
    }
    missing_metadata = not normalize(before["title"]) and not normalize(before["author"])

    if missing_metadata:
        evidence = verification.get("evidence") or {}
        embedded = evidence.get("embedded") or {}
        expected_signal = (evidence.get("content") or {}).get("expected_signal") or {}
        independently_verified = all(
            (
                confidence >= 97,
                bool(expected_signal.get("strong_identity")),
                bool(expected_signal.get("front_proximity")),
                not normalize(str(embedded.get("title") or "")),
                not normalize(str(embedded.get("author") or "")),
            )
        )
        if result.get("reason_code") != "NO_METADATA" or not independently_verified:
            return {
                "eligible": False,
                "safe": False,
                "kind": "EPUB_METADATA",
                "reason": (
                    "Missing EPUB metadata is filled only when the latest scan recorded "
                    "NO_METADATA and title-page-like content independently verifies the "
                    "expected title and author at 97% confidence or higher."
                ),
                "before": before,
                "after": {},
            }

        after = {
            "path": target,
            "title": result["title"],
            "author": result["author"],
        }
        return {
            "eligible": True,
            "safe": True,
            "kind": "EPUB_METADATA",
            "repair_reason_code": "MISSING_METADATA",
            "missing_metadata": True,
            "reason": (
                "The EPUB has no usable embedded title or author. Title-page-like "
                "content independently verifies the Bindery identity, so BookGuard "
                "can add the missing title and author."
            ),
            "before": before,
            "after": after,
            "verification_id": verification.get("id"),
            "verification_confidence": confidence,
        }

    after = {"path": target, "title": result["title"], "author": result["author"]}
    changed = normalize(before["title"]) != normalize(after["title"]) or normalize(before["author"]) != normalize(after["author"])
    return {
        "eligible": changed,
        "safe": changed,
        "kind": "EPUB_METADATA",
        "repair_reason_code": "CONFLICTING_METADATA",
        "missing_metadata": False,
        "reason": (
            "Internal book content independently verifies the expected title and author; only the conflicting EPUB metadata will be rewritten."
            if changed
            else "The EPUB metadata already matches the verified expected identity."
        ),
        "before": before,
        "after": after,
        "verification_id": verification.get("id"),
        "verification_confidence": confidence,
    }


def apply_verified_metadata_repair(result: dict) -> dict:
    require_current_scan_result(result)
    if settings.metadata_repair_mode != "safe":
        raise RepairError("Switch Metadata repair mode to Safe before writing verified metadata repairs.")

    # Always re-run verification immediately before a content-backed write.
    verification = verify_result(result, force=True)
    preview = verified_repair_preview(result, verification)
    if not preview.get("eligible") or not preview.get("safe"):
        raise RepairError(preview.get("reason") or "Verified repair safety check failed.")

    target = preview["after"]["path"]
    if not os.access(target, os.W_OK):
        raise RepairError("The ebook media mount is read-only or the EPUB is not writable.")

    require_current_scan_result(result)
    repair_id = create_metadata_repair(result, "EPUB_METADATA", preview["before"], preview["after"])
    try:
        apply_repair_changes(preview)
        verify_repair_changes(preview)
    except Exception as exc:
        finish_metadata_repair(repair_id, "failed", str(exc)[:1000])
        raise RepairError(str(exc)) from exc
    finish_metadata_repair(repair_id, "applied")
    return {"repair_id": repair_id, "verification": verification, **preview}


def verification_summary() -> dict:
    """Count only the newest verification for each result in the latest scan."""
    init_verification_db()
    scan = latest_scan()
    if not scan or not scan.get("id"):
        return {}
    with local_conn() as conn:
        rows = conn.execute(
            """
            SELECT cv.verdict, COUNT(*) AS n
            FROM content_verifications AS cv
            WHERE cv.scan_id = ?
              AND cv.id = (
                  SELECT cv2.id
                  FROM content_verifications AS cv2
                  WHERE cv2.scan_id = cv.scan_id
                    AND cv2.result_id = cv.result_id
                  ORDER BY cv2.updated_at DESC, cv2.id DESC
                  LIMIT 1
              )
            GROUP BY cv.verdict
            """,
            (scan["id"],),
        ).fetchall()
    return {str(row["verdict"]): int(row["n"]) for row in rows}


def verification_job_status() -> dict:
    with _job_lock:
        return json.loads(json.dumps(_job_state))


@serialized_start
def start_verification_job(classification: str = "REVIEW", reason_code: str | None = None,
                           *, check_id: str | None = None, expected_scan_id: str | None = None) -> str | None:
    from .scanner import scan_is_running

    if not start_allowed(check_id) or scan_is_running():
        return None
    classification = str(classification or "REVIEW").upper()
    if classification not in {"REVIEW", "REJECT", "ALL"}:
        raise ValueError("Verification jobs accept REVIEW, REJECT or ALL.")

    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise RuntimeError("A completed latest BookGuard scan is required before content verification.")
    if expected_scan_id and scan["id"] != expected_scan_id:
        raise RuntimeError("The latest scan changed before verification started.")

    with _job_lock:
        if _job_state.get("status") == "running":
            return None

    if classification == "ALL":
        rows = latest_review_results()
        if reason_code:
            rows = [row for row in rows if row.get("reason_code") == reason_code]
        states = triage_states(rows)
        rows = [row for row in rows if not states[int(row["id"])]["resolved"]]
    else:
        rows = latest_results(classification=classification, reason_code=reason_code, limit=10000)
        rows = [row for row in rows if not triage_state(row).get("resolved")]
    job_id = uuid.uuid4().hex

    with _job_lock:
        _job_state.update({
            "status": "running",
            "job_id": job_id,
            "classification": classification,
            "reason_code": reason_code,
            "total": len(rows),
            "processed": 0,
            "counts": {},
            "current": "",
            "error": None,
            "cacheHits": 0,
            "postponed": 0,
            "postponedReason": "",
            "elapsedSeconds": 0,
        })

    def worker() -> None:
        counts: dict[str, int] = {}
        cache_hits = 0
        postponed = 0
        job_started = time.monotonic()
        try:
            for index, row in enumerate(rows, start=1):
                with _job_lock:
                    _job_state["current"] = f"{row['author']} — {row['title']}"
                try:
                    verification = verify_result(row)
                except CatalogueUnavailable as exc:
                    # One unreadable catalogue lookup postpones this book only;
                    # the rest of the batch still runs. No record is written.
                    postponed += 1
                    with _job_lock:
                        _job_state["processed"] = index
                        _job_state["postponed"] = postponed
                        _job_state["postponedReason"] = str(exc)[:1000]
                        _job_state["elapsedSeconds"] = round(time.monotonic() - job_started)
                    continue
                verdict = str(verification.get("verdict") or "INSUFFICIENT_EVIDENCE")
                counts[verdict] = counts.get(verdict, 0) + 1
                if verification.get("cached"):
                    cache_hits += 1
                with _job_lock:
                    _job_state["processed"] = index
                    _job_state["counts"] = dict(counts)
                    _job_state["cacheHits"] = cache_hits
                    _job_state["elapsedSeconds"] = round(time.monotonic() - job_started)
            with _job_lock:
                _job_state["status"] = "complete"
                _job_state["current"] = ""
        except Exception as exc:
            with _job_lock:
                _job_state["status"] = "failed"
                _job_state["error"] = str(exc)[:1000]
                _job_state["current"] = ""

    threading.Thread(target=worker, name=f"bookguard-verifier-{job_id[:8]}", daemon=True).start()
    return job_id
