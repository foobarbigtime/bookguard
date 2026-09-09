from __future__ import annotations

import os
from pathlib import Path
import threading
import uuid

from .config import settings
from .db import add_result, create_scan, finish_scan, load_bindery_files, update_scan_progress
from .matcher import classify_audio, classify_ebook
from .metadata import audio_files, audio_metadata_summary, ebook_metadata, ffprobe_metadata


_scan_lock = threading.Lock()
_current_thread: threading.Thread | None = None

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


def _scan_one(row: dict) -> dict:
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
        classification, score, reason_code, reasons = classify_audio(
            row["title"], row["author"], samples
        )
        base.update(
            classification=classification,
            risk_score=score,
            reason_code=reason_code,
            reasons=reasons,
            metadata={"samples": samples, **summary},
        )
        return base

    target = local_path
    if os.path.isdir(local_path):
        candidates: list[Path] = []
        for candidate in Path(local_path).rglob("*"):
            if candidate.is_file() and candidate.suffix.lower() in EBOOK_CANDIDATE_SUFFIXES:
                candidates.append(candidate)
        if candidates:
            target = str(sorted(candidates, key=lambda path: str(path).casefold())[0])

    md = ebook_metadata(target)
    if "unsupported" in md:
        base.update(
            classification="REVIEW",
            risk_score=20,
            reason_code="UNSUPPORTED",
            reasons=[f"Ebook format {md['unsupported']} is not inspected yet."],
            metadata=md,
        )
        return base

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
    return base


def run_scan(scan_id: str, rows: list[dict]) -> None:
    try:
        total = len(rows)
        for i, row in enumerate(rows, start=1):
            add_result(scan_id, _scan_one(row))
            if i == total or i % settings.progress_every == 0:
                update_scan_progress(scan_id, i)
        finish_scan(scan_id)
    except Exception as exc:
        finish_scan(scan_id, status="failed", error=str(exc)[:1000])
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
        thread = threading.Thread(
            target=run_scan,
            args=(scan_id, rows),
            name="bookguard-scan",
            daemon=True,
        )
        _current_thread = thread
        thread.start()
        return scan_id
