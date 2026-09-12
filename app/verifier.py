from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from .config import settings
from .db import (
    create_metadata_repair,
    finish_metadata_repair,
    latest_results,
    latest_scan,
    local_conn,
)
from .matcher import normalize
from .metadata import ebook_metadata
from .repair import RepairError, _apply_preview, _verify_preview
from .triage import result_signature, triage_state
from .tika_client import extract_text as _tika_text
from .tika_client import test_connection as test_tika
from .verification_engine import (
    FRONT_TEXT_CHARS,
    MIN_USEFUL_TEXT,
    _classify_identity,
    _epub_identity,
    _pdf_identity,
    _plain_identity,
)


VERIFIER_VERSION = "6"
VERDICTS = {
    "VERIFIED_CORRECT",
    "METADATA_ERROR",
    "WRONG_CONTENT",
    "INSUFFICIENT_EVIDENCE",
}
EBOOK_SUFFIXES = {
    ".epub", ".pdf", ".mobi", ".azw", ".azw3", ".cbz", ".rtf", ".txt", ".cbr", ".lit"
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
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _resolve_target(local_path: str) -> str:
    path = Path(local_path)
    if path.is_file():
        return str(path)
    if not path.is_dir():
        return str(path)
    candidates = sorted(
        (
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in EBOOK_SUFFIXES
        ),
        key=lambda candidate: str(candidate).casefold(),
    )
    return str(candidates[0]) if candidates else str(path)


def _file_fingerprint(path: str) -> str:
    target = Path(path)
    if not target.is_file():
        return "missing"
    stat = target.stat()
    digest = hashlib.sha256()
    digest.update(str(target).encode("utf-8", errors="replace"))
    digest.update(str(stat.st_size).encode())
    digest.update(str(stat.st_mtime_ns).encode())
    try:
        with target.open("rb") as fh:
            digest.update(fh.read(65536))
            if stat.st_size > 65536:
                fh.seek(max(0, stat.st_size - 65536))
                digest.update(fh.read(65536))
    except OSError:
        pass
    return digest.hexdigest()


def _verification_signature(result: dict, target_path: str, fingerprint: str) -> str:
    raw = "|".join(
        [VERIFIER_VERSION, result_signature(result), target_path, fingerprint]
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _decode_row(row) -> dict:
    item = dict(row)
    item["evidence"] = json.loads(item.pop("evidence_json"))
    item["cached"] = True
    return item


def verification_for_result(result: dict) -> dict | None:
    init_verification_db()
    target = _resolve_target(result.get("local_path") or "")
    fingerprint = _file_fingerprint(target)
    signature = _verification_signature(result, target, fingerprint)
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM content_verifications WHERE signature=? LIMIT 1",
            (signature,),
        ).fetchone()
    return _decode_row(row) if row else None


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
    signature = _verification_signature(result, target_path, fingerprint)
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


def verify_result(result: dict, force: bool = False) -> dict:
    if not settings.verification_enabled:
        raise RuntimeError("Content verification is disabled in Settings.")

    target = _resolve_target(result.get("local_path") or "")
    fingerprint = _file_fingerprint(target)
    if not force:
        cached = verification_for_result(result)
        if cached:
            return cached

    if result.get("format") != "ebook":
        evidence = {
            "expected": {"title": result.get("title", ""), "author": result.get("author", "")},
            "embedded": {},
            "content": {},
            "metadata_matches_expected": False,
            "notes": ["Audiobook content verification is not implemented yet; existing tag-based scanning remains in use."],
            "explanation": "This verifier currently establishes book identity from ebook content only.",
        }
        return _save_verification(
            result, target, fingerprint, "INSUFFICIENT_EVIDENCE", 0, "unsupported-audiobook", evidence
        )

    path = Path(target)
    if not path.is_file():
        evidence = {
            "expected": {"title": result.get("title", ""), "author": result.get("author", "")},
            "embedded": {},
            "content": {},
            "metadata_matches_expected": False,
            "notes": ["The tracked ebook target could not be opened as a file."],
            "explanation": "No readable ebook file was available for content verification.",
        }
        return _save_verification(
            result, target, fingerprint, "INSUFFICIENT_EVIDENCE", 0, "missing", evidence
        )

    suffix = path.suffix.lower()
    metadata: dict = ebook_metadata(str(path))
    text = ""
    front_text = ""
    identifiers: list[str] = []
    source = "native"
    notes: list[str] = []

    try:
        if suffix == ".epub":
            metadata, text, identifiers, front_text = _epub_identity(str(path))
            source = "native-epub"
        elif suffix == ".pdf":
            metadata, text, identifiers, front_text = _pdf_identity(str(path))
            source = "native-pdf"
        elif suffix in {".txt", ".rtf"}:
            metadata, text, identifiers, front_text = _plain_identity(str(path))
            source = f"native-{suffix.lstrip('.')}"
    except Exception as exc:
        notes.append(f"Native extraction error: {str(exc)[:300]}")

    # Optional Tika fallback only when native extraction did not provide useful text.
    if (
        len(text.strip()) < MIN_USEFUL_TEXT
        and settings.verification_use_tika
        and settings.verification_tika_url
    ):
        tika_text, tika_error = _tika_text(str(path))
        if tika_text:
            text = tika_text
            front_text = tika_text[:FRONT_TEXT_CHARS]
            source = f"{source}+tika" if source != "native" else "tika"
            notes.append("Tika fallback supplied content because native extraction was insufficient.")
        elif tika_error:
            notes.append(tika_error)

    verdict, confidence, evidence = _classify_identity(
        result, metadata, text, identifiers, source, notes, front_text
    )
    return _save_verification(
        result, str(path), fingerprint, verdict, confidence, source, evidence
    )


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
    if verification.get("verdict") != "METADATA_ERROR" or int(verification.get("confidence") or 0) < 90:
        return {
            "eligible": False,
            "safe": False,
            "kind": "EPUB_METADATA",
            "reason": "Only high-confidence METADATA_ERROR verdicts can use verified metadata repair.",
            "before": {},
            "after": {},
        }

    target = _resolve_target(result.get("local_path") or "")
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
    before = {
        "path": target,
        "title": str(current.get("title") or ""),
        "author": str(current.get("author") or ""),
    }
    after = {"path": target, "title": result["title"], "author": result["author"]}
    changed = normalize(before["title"]) != normalize(after["title"]) or normalize(before["author"]) != normalize(after["author"])
    return {
        "eligible": changed,
        "safe": changed,
        "kind": "EPUB_METADATA",
        "reason": (
            "Internal book content independently verifies the expected title and author; only the conflicting EPUB metadata will be rewritten."
            if changed
            else "The EPUB metadata already matches the verified expected identity."
        ),
        "before": before,
        "after": after,
        "verification_id": verification.get("id"),
        "verification_confidence": verification.get("confidence"),
    }


def apply_verified_metadata_repair(result: dict) -> dict:
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

    repair_id = create_metadata_repair(result, "EPUB_METADATA", preview["before"], preview["after"])
    try:
        _apply_preview(preview)
        _verify_preview(preview)
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


def start_verification_job(classification: str = "REVIEW", reason_code: str | None = None) -> str | None:
    classification = str(classification or "REVIEW").upper()
    if classification not in {"REVIEW", "REJECT"}:
        raise ValueError("Verification jobs currently accept REVIEW or REJECT.")

    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise RuntimeError("A completed latest BookGuard scan is required before content verification.")

    with _job_lock:
        if _job_state.get("status") == "running":
            return None

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
        })

    def worker() -> None:
        counts: dict[str, int] = {}
        try:
            for index, row in enumerate(rows, start=1):
                with _job_lock:
                    _job_state["current"] = f"{row['author']} — {row['title']}"
                verification = verify_result(row)
                verdict = str(verification.get("verdict") or "INSUFFICIENT_EVIDENCE")
                counts[verdict] = counts.get(verdict, 0) + 1
                with _job_lock:
                    _job_state["processed"] = index
                    _job_state["counts"] = dict(counts)
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
