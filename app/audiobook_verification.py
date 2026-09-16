from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .metadata import AUDIO_EXTENSIONS


MIN_REASONABLE_AUDIO_BYTES = 4096


def discover_audio_files(path: str) -> list[str]:
    """Return every supported audio file below path in deterministic order."""
    root = Path(path)
    if root.is_file():
        return [str(root)] if root.suffix.lower() in AUDIO_EXTENSIONS else []
    if not root.is_dir():
        return []

    found: list[str] = []
    for current_root, _, names in os.walk(root):
        for name in names:
            candidate = Path(current_root) / name
            if candidate.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(str(candidate))
    return sorted(found, key=str.casefold)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _integer(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def probe_audio_file(path: str) -> dict[str, Any]:
    """Collect technical stream/container information without modifying the file."""
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        return {"path": path, "probe_error": str(exc)[:500], "size_bytes": None}

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        (
            "format=format_name,duration,bit_rate:"
            "stream=index,codec_type,codec_name,sample_rate,channels,bit_rate,duration:"
            "chapter=id,start_time,end_time"
        ),
        "-of", "json",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"path": path, "probe_error": str(exc)[:500], "size_bytes": size}

    if proc.returncode != 0:
        return {
            "path": path,
            "probe_error": proc.stderr.strip()[:500] or f"ffprobe exited with {proc.returncode}",
            "size_bytes": size,
        }
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"path": path, "probe_error": "ffprobe returned invalid JSON", "size_bytes": size}

    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    chapters = payload.get("chapters") if isinstance(payload.get("chapters"), list) else []
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    primary = audio_streams[0] if audio_streams else {}

    duration = _number(fmt.get("duration"))
    if duration is None:
        duration = _number(primary.get("duration"))

    return {
        "path": path,
        "size_bytes": size,
        "format_name": str(fmt.get("format_name") or ""),
        "duration_seconds": duration,
        "bit_rate": _integer(primary.get("bit_rate")) or _integer(fmt.get("bit_rate")),
        "audio_stream_count": len(audio_streams),
        "codec": str(primary.get("codec_name") or ""),
        "sample_rate": _integer(primary.get("sample_rate")),
        "channels": _integer(primary.get("channels")),
        "chapter_count": len(chapters),
    }


def _distinct(values: list[Any]) -> list[Any]:
    return sorted({value for value in values if value not in (None, "")}, key=str)


def summarize_audiobook_verification(probes: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn per-file probe results into a deterministic read-only verification result."""
    if not probes:
        return {
            "verdict": "FAIL",
            "reason_code": "NO_AUDIO_FILES",
            "reasons": ["No supported audio files were found."],
            "file_count": 0,
            "readable_file_count": 0,
            "total_duration_seconds": 0.0,
            "files": [],
        }

    failures: list[str] = []
    warnings: list[str] = []
    readable: list[dict[str, Any]] = []

    for probe in probes:
        path = str(probe.get("path") or "")
        name = Path(path).name or path or "unknown file"
        size = probe.get("size_bytes")
        if size == 0:
            failures.append(f"{name}: file is zero bytes.")
        elif isinstance(size, int) and 0 < size < MIN_REASONABLE_AUDIO_BYTES:
            warnings.append(f"{name}: file is unusually small ({size} bytes).")

        if probe.get("probe_error"):
            failures.append(f"{name}: ffprobe could not read the file ({probe['probe_error']}).")
            continue
        if int(probe.get("audio_stream_count") or 0) < 1:
            failures.append(f"{name}: container has no audio stream.")
            continue

        duration = _number(probe.get("duration_seconds"))
        if duration is None or duration <= 0:
            failures.append(f"{name}: audio duration is missing or zero.")
            continue
        readable.append(probe)

    if readable:
        codecs = _distinct([probe.get("codec") for probe in readable])
        sample_rates = _distinct([probe.get("sample_rate") for probe in readable])
        channels = _distinct([probe.get("channels") for probe in readable])
        if len(codecs) > 1:
            warnings.append(f"Audiobook uses multiple audio codecs: {', '.join(map(str, codecs))}.")
        if len(sample_rates) > 1:
            warnings.append(
                f"Audiobook uses multiple sample rates: {', '.join(map(str, sample_rates))} Hz."
            )
        if len(channels) > 1:
            warnings.append(
                f"Audiobook uses multiple channel layouts/counts: {', '.join(map(str, channels))}."
            )

    single_m4b = (
        len(probes) == 1
        and Path(str(probes[0].get("path") or "")).suffix.lower() == ".m4b"
        and not probes[0].get("probe_error")
    )
    if single_m4b and int(probes[0].get("chapter_count") or 0) == 0:
        warnings.append("Single-file M4B has no chapter table; audio may still be playable.")

    total_duration = sum(
        _number(probe.get("duration_seconds")) or 0.0
        for probe in readable
    )

    if failures:
        verdict = "FAIL"
        reason_code = "AUDIO_INTEGRITY_FAILED"
        reasons = failures + warnings
    elif warnings:
        verdict = "REVIEW"
        reason_code = "AUDIO_TECHNICAL_WARNING"
        reasons = warnings
    else:
        verdict = "PASS"
        reason_code = "AUDIO_TECHNICAL_PASS"
        reasons = ["All discovered audio files passed structural technical checks."]

    return {
        "verdict": verdict,
        "reason_code": reason_code,
        "reasons": reasons,
        "file_count": len(probes),
        "readable_file_count": len(readable),
        "total_duration_seconds": round(total_duration, 3),
        "codecs": _distinct([probe.get("codec") for probe in readable]),
        "sample_rates": _distinct([probe.get("sample_rate") for probe in readable]),
        "channels": _distinct([probe.get("channels") for probe in readable]),
        "chapter_count": sum(int(probe.get("chapter_count") or 0) for probe in readable),
        "files": probes,
    }


def verify_audiobook(path: str) -> dict[str, Any]:
    files = discover_audio_files(path)
    probes = [probe_audio_file(candidate) for candidate in files]
    return summarize_audiobook_verification(probes)
