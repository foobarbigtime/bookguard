from __future__ import annotations

import os
from pathlib import Path
import threading
import uuid

from .audiobook_verification import AudiobookVerificationCancelled, verify_audiobook
from .config import settings
from .db import add_result, create_scan, finish_scan, load_bindery_files, update_scan_progress
from .language_detection import LANGUAGE_NAMES, ebook_languages, normalize_language, outside_library_languages
from .matcher import classify_audio, classify_ebook
from .media_discovery import representative_items, resolve_ebook_target
from .metadata import audio_metadata_summary, ebook_metadata
from .series_titles import bindery_series_names


_scan_lock = threading.Lock()
_detail_lock = threading.Lock()
_current_thread: threading.Thread | None = None
_current_detail: dict[str, object] = {}
_safe_cancel_event = threading.Event()
_immediate_stop_event = threading.Event()


def map_path(stored_path: str, fmt: str) -> str:
    if fmt == "audiobook":
        prefix = settings.audiobook_bindery_prefix
        root = settings.audiobook_root
    else:
        prefix = settings.ebook_bindery_prefix
        root = settings.ebook_root
    if stored_path == prefix:
        return root
    if stored_path.startswith(prefix + "/"):
        return os.path.join(root, stored_path[len(prefix) + 1 :])
    return stored_path


def _set_detail(**values: object) -> None:
    with _detail_lock:
        _current_detail.update(values)


def current_scan_detail(scan_id: str | None = None) -> dict[str, object]:
    with _detail_lock:
        detail = dict(_current_detail)
    if scan_id and detail.get("scan_id") != scan_id:
        return {}
    return detail


def scan_is_running() -> bool:
    with _scan_lock:
        return bool(_current_thread and _current_thread.is_alive())


def request_safe_cancel() -> bool:
    """Finish the current book, persist it, then stop before the next book."""
    if not scan_is_running():
        return False
    _safe_cancel_event.set()
    _set_detail(cancel_mode="safe", cancel_requested=True)
    return True


def request_immediate_stop() -> bool:
    """Stop as quickly as possible and interrupt active audiobook ffprobe work."""
    if not scan_is_running():
        return False
    _immediate_stop_event.set()
    _set_detail(cancel_mode="immediate", cancel_requested=True, phase="stopping")
    return True


def _raise_if_immediate_stop() -> None:
    if _immediate_stop_event.is_set():
        raise AudiobookVerificationCancelled("Scan stopped immediately by user.")


def _language_override(base: dict, language_result: dict) -> dict:
    other = outside_library_languages(language_result)
    if not other:
        return base

    labels = ", ".join(LANGUAGE_NAMES.get(code, code) for code in other)
    message = (
        f"The file declares {labels}, which is not one of your library languages "
        "(Settings → Scanning). Flagged for removal."
    )
    metadata = dict(base.get("metadata") or {})
    policy_flags = {
        str(flag)
        for flag in metadata.get("policy_flags", [])
        if str(flag).strip()
    }
    policy_flags.add("NON_ENGLISH_LANGUAGE")
    metadata["policy_flags"] = sorted(policy_flags)
    base["metadata"] = metadata
    base["risk_score"] = max(int(base.get("risk_score") or 0), 90)
    base["reasons"] = [*list(base.get("reasons") or []), message]

    # Language is an additional policy signal. It may escalate a PASS/REVIEW
    # result to REVIEW, but it must never weaken a stronger integrity/identity
    # finding such as REJECT by replacing its classification or reason code.
    if base.get("classification") != "REJECT":
        base["classification"] = "REVIEW"
        base["reason_code"] = "NON_ENGLISH_LANGUAGE"
    return base


def _audiobook_language_from_probes(probes: list[dict], sample_limit: int) -> dict:
    sampled = representative_items(probes, max(1, sample_limit))
    languages: set[str] = set()
    evidence: list[dict] = []
    for probe in sampled:
        language = normalize_language(probe.get("language"))
        if language:
            languages.add(language)
            evidence.append({"path": probe.get("path"), "languages": [language]})
    return {
        "source": "cached_embedded_audio_metadata",
        "languages": sorted(languages),
        "evidence": evidence,
        "sampled_files": len(sampled),
    }


def _scan_one(row: dict, audiobook_progress=None) -> dict:
    _raise_if_immediate_stop()
    local_path = map_path(row["stored_path"], row["format"])
    base = {
        **row,
        "local_path": local_path,
        "metadata": {},
        "reason_code": "UNKNOWN",
        "reasons": [],
    }

    if not os.path.exists(local_path):
        base.update(
            classification="MISSING",
            risk_score=100,
            reason_code="MISSING",
            reasons=["Bindery tracks this path, but it does not exist on disk."],
        )
        return base

    if row["format"] == "audiobook":
        technical = verify_audiobook(
            local_path,
            progress_callback=audiobook_progress,
            cancel_check=_immediate_stop_event.is_set,
        )
        _raise_if_immediate_stop()
        probes = list(technical.get("files") or [])
        samples = representative_items(probes, settings.sample_files)
        summary = audio_metadata_summary(samples)
        language = _audiobook_language_from_probes(probes, settings.sample_files)
        classification, score, reason_code, reasons = classify_audio(
            row["title"], row["author"], probes
        )
        base.update(
            classification=classification,
            risk_score=score,
            reason_code=reason_code,
            reasons=reasons,
            metadata={
                "samples": samples,
                **summary,
                "technical_verification": technical,
                "language_detection": language,
            },
        )
        return _language_override(base, language)

    target = resolve_ebook_target(
        local_path,
        check=_raise_if_immediate_stop,
    )

    _raise_if_immediate_stop()
    md = ebook_metadata(target)
    _raise_if_immediate_stop()
    language = ebook_languages(target)
    md["language_detection"] = language
    if "unsupported" in md:
        base.update(
            classification="REVIEW",
            risk_score=20,
            reason_code="UNSUPPORTED",
            reasons=[f"Ebook format {md['unsupported']} is not inspected yet."],
            metadata=md,
        )
        return _language_override(base, language)

    classification, score, reason_code, reasons = classify_ebook(
        row["title"], row["author"], md,
        series_names=bindery_series_names(row.get("book_id")),
    )
    if md.get("error") and reason_code == "NO_METADATA":
        reasons = [*reasons, f"Metadata parser reported: {md['error']}"]
    base.update(
        classification=classification,
        risk_score=score,
        reason_code=reason_code,
        reasons=reasons,
        metadata=md,
    )
    return _language_override(base, language)


def _mark_cancelled(scan_id: str, *, status: str, phase: str, message: str) -> None:
    finish_scan(scan_id, status=status)
    _set_detail(
        scan_id=scan_id,
        phase=phase,
        cancel_requested=False,
        cancel_mode=None,
        message=message,
        audio_file_index=None,
        audio_file_total=None,
        current_file=None,
    )


def run_scan(scan_id: str, rows: list[dict]) -> None:
    try:
        total = len(rows)
        for i, row in enumerate(rows, start=1):
            if _immediate_stop_event.is_set():
                _mark_cancelled(
                    scan_id,
                    status="stopped",
                    phase="stopped",
                    message="Scan stopped immediately by user.",
                )
                return
            if _safe_cancel_event.is_set():
                _mark_cancelled(
                    scan_id,
                    status="cancelled",
                    phase="cancelled",
                    message="Scan cancelled safely before starting the next book.",
                )
                return

            base_detail = {
                "scan_id": scan_id,
                "phase": "scanning",
                "book_index": i,
                "book_total": total,
                "book_id": row.get("book_id"),
                "title": row.get("title"),
                "author": row.get("author"),
                "format": row.get("format"),
                "audio_file_index": None,
                "audio_file_total": None,
                "current_file": None,
                "cache_hits": 0,
                "cache_misses": 0,
                "cancel_requested": False,
                "cancel_mode": None,
                "message": None,
            }
            _set_detail(**base_detail)

            def audiobook_progress(event: dict) -> None:
                path = str(event.get("path") or "")
                detail = {
                    **base_detail,
                    "phase": str(event.get("phase") or "scanning"),
                    "audio_file_index": event.get("file_index"),
                    "audio_file_total": event.get("file_total"),
                    "current_file": Path(path).name if path else None,
                    "cache_hits": int(event.get("cache_hits") or 0),
                    "cache_misses": int(event.get("cache_misses") or 0),
                    "cancel_requested": _safe_cancel_event.is_set() or _immediate_stop_event.is_set(),
                    "cancel_mode": (
                        "immediate" if _immediate_stop_event.is_set()
                        else "safe" if _safe_cancel_event.is_set()
                        else None
                    ),
                }
                _set_detail(**detail)

            result = _scan_one(
                row,
                audiobook_progress=audiobook_progress if row["format"] == "audiobook" else None,
            )
            _raise_if_immediate_stop()
            add_result(scan_id, result)
            update_scan_progress(scan_id, i)

            if _safe_cancel_event.is_set():
                _mark_cancelled(
                    scan_id,
                    status="cancelled",
                    phase="cancelled",
                    message="Safe cancel completed after finishing the current book.",
                )
                return

        finish_scan(scan_id)
        _set_detail(
            scan_id=scan_id,
            phase="complete",
            book_index=total,
            book_total=total,
            audio_file_index=None,
            audio_file_total=None,
            current_file=None,
            cancel_requested=False,
            cancel_mode=None,
            message="Scan completed.",
        )
    except AudiobookVerificationCancelled:
        _mark_cancelled(
            scan_id,
            status="stopped",
            phase="stopped",
            message="Scan stopped immediately by user. The interrupted book was not saved as a completed result.",
        )
    except Exception as exc:
        finish_scan(scan_id, status="failed", error=str(exc)[:1000])
        _set_detail(scan_id=scan_id, phase="failed", error=str(exc)[:500])
    finally:
        _safe_cancel_event.clear()
        _immediate_stop_event.clear()
        global _current_thread
        with _scan_lock:
            _current_thread = None


def start_scan() -> str | None:
    global _current_thread
    with _scan_lock:
        if _current_thread and _current_thread.is_alive():
            return None
        _safe_cancel_event.clear()
        _immediate_stop_event.clear()
        rows = load_bindery_files()
        rows = [
            row for row in rows
            if (row["format"] == "audiobook" and settings.scan_audiobooks)
            or (row["format"] != "audiobook" and settings.scan_ebooks)
        ]
        scan_id = uuid.uuid4().hex
        create_scan(scan_id, len(rows))
        _set_detail(
            scan_id=scan_id,
            phase="starting",
            book_index=0,
            book_total=len(rows),
            audio_file_index=None,
            audio_file_total=None,
            current_file=None,
            cache_hits=0,
            cache_misses=0,
            cancel_requested=False,
            cancel_mode=None,
            message=None,
        )
        thread = threading.Thread(
            target=run_scan,
            args=(scan_id, rows),
            name="bookguard-scan",
            daemon=True,
        )
        _current_thread = thread
        thread.start()
        return scan_id
