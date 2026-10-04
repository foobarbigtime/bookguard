from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .audiobook_probe_cache import cached_probe, save_probe
from .media_discovery import discover_audio_files
from .subprocess_capture import ProcessCancelled, ProcessOutputLimitExceeded, run_bounded_process


MIN_REASONABLE_AUDIO_BYTES = 4096
FFPROBE_TIMEOUT_SECONDS = 60
FFPROBE_STDOUT_LIMIT_BYTES = 1024 * 1024
FFPROBE_STDERR_LIMIT_BYTES = 64 * 1024
DISC_TRACK_SUFFIX = re.compile(
    r"^(?P<prefix>.*?)(?P<disc>\d{1,3})[-_](?P<track>\d{2,3})$",
    flags=re.IGNORECASE,
)

ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


class AudiobookVerificationCancelled(RuntimeError):
    """Raised when an in-progress audiobook verification is stopped by the user."""


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


def _normalized_tags(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key).lower(): item for key, item in value.items()}


def probe_audio_file(
    path: str,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Collect technical and embedded metadata without modifying the file."""
    source = Path(path)
    try:
        stat = source.stat()
        size = stat.st_size
    except OSError as exc:
        return {"path": path, "probe_error": str(exc)[:500], "size_bytes": None}

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        (
            "format=format_name,duration,bit_rate:"
            "format_tags=artist,album_artist,author,composer,narrator,performer,album,title,genre,language,track,disc:"
            "stream=index,codec_type,codec_name,sample_rate,channels,bit_rate,duration:"
            "stream_tags=artist,album_artist,author,composer,narrator,performer,album,title,genre,language,track,disc:"
            "chapter=id,start_time,end_time"
        ),
        "-of", "json",
        path,
    ]
    try:
        proc = run_bounded_process(
            cmd,
            timeout=FFPROBE_TIMEOUT_SECONDS,
            stdout_limit=FFPROBE_STDOUT_LIMIT_BYTES,
            stderr_limit=FFPROBE_STDERR_LIMIT_BYTES,
            cancel_check=cancel_check,
        )
    except ProcessCancelled:
        raise AudiobookVerificationCancelled("Audiobook verification stopped by user.")
    except subprocess.TimeoutExpired:
        return {
            "path": path,
            "probe_error": f"ffprobe timed out after {FFPROBE_TIMEOUT_SECONDS} seconds",
            "size_bytes": size,
        }
    except ProcessOutputLimitExceeded as exc:
        return {
            "path": path,
            "probe_error": f"ffprobe {exc.stream} exceeded the {exc.limit}-byte output limit",
            "size_bytes": size,
        }
    except OSError as exc:
        return {"path": path, "probe_error": str(exc)[:500], "size_bytes": size}

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        return {
            "path": path,
            "probe_error": stderr.strip()[:500] or f"ffprobe exited with {proc.returncode}",
            "size_bytes": size,
        }
    try:
        payload = json.loads(stdout or "{}")
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

    fmt_tags = _normalized_tags(fmt.get("tags"))
    stream_tags = _normalized_tags(primary.get("tags"))

    def tag(name: str) -> str:
        return str(fmt_tags.get(name) or stream_tags.get(name) or "")

    return {
        "path": path,
        "size_bytes": size,
        "modified_ns": stat.st_mtime_ns,
        "format_name": str(fmt.get("format_name") or ""),
        "duration_seconds": duration,
        "duration": str(duration) if duration is not None else "",
        "bit_rate": _integer(primary.get("bit_rate")) or _integer(fmt.get("bit_rate")),
        "audio_stream_count": len(audio_streams),
        "codec": str(primary.get("codec_name") or ""),
        "sample_rate": _integer(primary.get("sample_rate")),
        "channels": _integer(primary.get("channels")),
        "chapter_count": len(chapters),
        "artist": tag("artist"),
        "album_artist": tag("album_artist"),
        "author": tag("author"),
        "composer": tag("composer"),
        "narrator": tag("narrator"),
        "performer": tag("performer"),
        "album": tag("album"),
        "title": tag("title"),
        "genre": tag("genre"),
        "language": tag("language"),
        "track": tag("track"),
        "disc": tag("disc"),
    }


def cached_or_probe_audio_file(
    path: str,
    cancel_check: CancelCheck | None = None,
) -> tuple[dict[str, Any], bool]:
    """Reuse a successful probe while path, size and mtime remain unchanged."""
    source = Path(path)
    try:
        stat = source.stat()
    except OSError:
        return probe_audio_file(path, cancel_check=cancel_check), False

    cached = cached_probe(path, stat.st_size, stat.st_mtime_ns)
    if cached is not None:
        cached["cache_hit"] = True
        return cached, True

    probe = probe_audio_file(path, cancel_check=cancel_check)
    probe["cache_hit"] = False
    if not probe.get("probe_error"):
        save_probe(path, stat.st_size, stat.st_mtime_ns, probe)
    return probe, False


def _distinct(values: list[Any]) -> list[Any]:
    return sorted({value for value in values if value not in (None, "")}, key=str)


def _missing_in_range(values: list[int]) -> list[int]:
    if not values:
        return []
    present = set(values)
    return [value for value in range(min(values), max(values) + 1) if value not in present]


def disc_track_sequence_warnings(probes: list[dict[str, Any]]) -> list[str]:
    """Detect strong filename evidence of missing audiobook disc/track parts."""
    if len(probes) < 2:
        return []

    parsed: list[tuple[str, int, int]] = []
    for probe in probes:
        stem = Path(str(probe.get("path") or "")).stem
        match = DISC_TRACK_SUFFIX.match(stem)
        if not match:
            return []
        prefix = re.sub(r"[\s._-]+$", "", match.group("prefix")).casefold()
        parsed.append((prefix, int(match.group("disc")), int(match.group("track"))))

    prefixes = {prefix for prefix, _, _ in parsed}
    if len(prefixes) != 1:
        return []

    # "Title 01-35", "Title 02-35", ...: every file ends in the same total, so the
    # numbers mean "part 1 of 35", not disc 1, track 35.
    totals = {track for _, _, track in parsed}
    parts = sorted({disc for _, disc, _ in parsed})
    if len(totals) == 1 and next(iter(totals)) >= parts[-1] > 1:
        total = next(iter(totals))
        missing = [str(n) for n in range(1, total + 1) if n not in parts]
        if missing:
            return [f"Parts {', '.join(missing[:20])} of {total} are missing."]
        return []

    groups: dict[int, list[int]] = {}
    for _, disc, track in parsed:
        groups.setdefault(disc, []).append(track)

    warnings: list[str] = []
    discs = sorted(groups)
    first_disc = discs[0]
    if first_disc > 1:
        missing = ", ".join(str(value) for value in range(1, first_disc))
        warnings.append(
            f"Filename sequence starts at disc/part {first_disc}; earlier disc/part "
            f"numbers are absent ({missing})."
        )

    missing_discs = _missing_in_range(discs)
    if missing_discs:
        warnings.append(
            "Filename sequence has missing disc/part numbers: "
            + ", ".join(map(str, missing_discs))
            + "."
        )

    for disc in discs:
        tracks = sorted(set(groups[disc]))
        first_track = tracks[0]
        if first_track > 1:
            warnings.append(
                f"Disc/part {disc} filename sequence starts at track {first_track}; "
                "earlier track numbers are absent."
            )
        missing_tracks = _missing_in_range(tracks)
        if missing_tracks:
            warnings.append(
                f"Disc/part {disc} filename sequence has missing track numbers: "
                + ", ".join(map(str, missing_tracks))
                + "."
            )

    return warnings


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
            "cache_hits": 0,
            "cache_misses": 0,
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

    warnings.extend(disc_track_sequence_warnings(probes))

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

    cache_hits = sum(1 for probe in probes if probe.get("cache_hit") is True)
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
        "cache_hits": cache_hits,
        "cache_misses": len(probes) - cache_hits,
        "files": probes,
    }


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    phase: str,
    file_index: int,
    file_total: int,
    path: str | None,
    cache_hits: int = 0,
    cache_misses: int = 0,
) -> None:
    if callback is None:
        return
    callback(
        {
            "phase": phase,
            "file_index": file_index,
            "file_total": file_total,
            "path": path,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
        }
    )


def _raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check and cancel_check():
        raise AudiobookVerificationCancelled("Audiobook verification stopped by user.")


def verify_audio_files(
    files: list[str],
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Verify an explicit deterministic file set, including renamed audio containers."""
    _raise_if_cancelled(cancel_check)
    files = sorted({str(path) for path in files}, key=str.casefold)
    total = len(files)
    _emit_progress(
        progress_callback,
        phase="discovered",
        file_index=0,
        file_total=total,
        path=None,
    )

    probes: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    for index, candidate in enumerate(files, start=1):
        _raise_if_cancelled(cancel_check)
        probe, hit = cached_or_probe_audio_file(candidate, cancel_check=cancel_check)
        if hit:
            cache_hits += 1
            phase = "cached"
        else:
            cache_misses += 1
            phase = "probing"
        probes.append(probe)
        _emit_progress(
            progress_callback,
            phase=phase,
            file_index=index,
            file_total=total,
            path=candidate,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        )

    _raise_if_cancelled(cancel_check)
    _emit_progress(
        progress_callback,
        phase="summarizing",
        file_index=total,
        file_total=total,
        path=None,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
    )
    result = summarize_audiobook_verification(probes)
    _emit_progress(
        progress_callback,
        phase="complete",
        file_index=total,
        file_total=total,
        path=None,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
    )
    return result



def verify_audiobook(
    path: str,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Verify all conventionally named audio files below one audiobook path."""
    return verify_audio_files(
        discover_audio_files(path),
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
