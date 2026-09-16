from __future__ import annotations

import os
from pathlib import Path
import threading
import uuid

from .audiobook_verification import verify_audiobook
from .config import settings
from .db import add_result, create_scan, finish_scan, load_bindery_files, update_scan_progress
from .language_detection import audiobook_languages, ebook_languages, explicit_non_english
from .matcher import classify_audio, classify_ebook
from .metadata import audio_files, audio_metadata_summary, ebook_metadata, ffprobe_metadata


_scan_lock = threading.Lock()
_detail_lock = threading.Lock()
_current_thread: threading.Thread | None = None
_current_detail: dict[str, object] = {}

EBOOK_CANDIDATE_SUFFIXES = {
    ".epub", ".pdf", ".mobi", ".azw", ".azw3", ".cbz", ".rtf", ".txt",
    ".cbr", ".lit",
}


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


def _language_override(base: dict, language_result: dict) -> dict:
    non_english = explicit_non_english(language_result)
    if not non_english:
        return base
    labels = ", ".join(non_english)
    base.update(
        classification="REVIEW",
        risk_score=max(int(base.get("risk_score") or 0), 90),
        reason_code="NON_ENGLISH_LANGUAGE",
        reasons=[
            f"Embedded metadata explicitly identifies non-English language: {labels}. "
            "Flagged for review/removal."
        ],
    )
    return base


def _scan_one(row: dict, audiobook_progress=None) -> dict:
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
        samples = [ffprobe_metadata(path) for path in audio_files(local_path, settings.sample_files)]
        summary = audio_metadata_summary(samples)
        technical = verify_audiobook(local_path, progress_callback=audiobook_progress)
        language = audiobook_languages(local_path, sample_limit=settings.sample_files)
        classification, score, reason_code, reasons = classify_audio(
            row["title"], row["author"], samples
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

    target = local_path
    if os.path.isdir(local_path):
        candidates: list[Path] = []
        for candidate in Path(local_path).rglob("*"):
            if candidate.is_file() and candidate.suffix.lower() in EBOOK_CANDIDATE_SUFFIXES:
                candidates.append(candidate)
        if candidates:
            target = str(sorted(candidates, key=lambda path: str(path).casefold())[0])

    md = ebook_metadata(target)
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
        row["title"], row["author"], md
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


def run_scan(scan_id: str, rows: list[dict]) -> None:
    try:
        total = len(rows)
        for i, row in enumerate(rows, start=1):
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
                }
                _set_detail(**detail)

            add_result(
                scan_id,
                _scan_one(
                    row,
                    audiobook_progress=audiobook_progress if row["format"] == "audiobook" else None,
                ),
            )
            update_scan_progress(scan_id, i)

        finish_scan(scan_id)
        _set_detail(
            scan_id=scan_id,
            phase="complete",
            book_index=total,
            book_total=total,
            audio_file_index=None,
            audio_file_total=None,
            current_file=None,
        )
    except Exception as exc:
        finish_scan(scan_id, status="failed", error=str(exc)[:1000])
        _set_detail(scan_id=scan_id, phase="failed", error=str(exc)[:500])
    finally:
        global _current_thread
        with _scan_lock:
            _current_thread = None


def start_scan() -> str | None:
    global _current_thread
    with _scan_lock:
        if _current_thread and _current_thread.is_alive():
            return None
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
