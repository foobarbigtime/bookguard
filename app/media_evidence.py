from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import zipfile
from typing import Any

from .audiobook_verification import probe_audio_file
from .media_discovery import AUDIO_EXTENSIONS, EBOOK_EXTENSIONS


MEDIA_CANDIDATE_EXTENSIONS = AUDIO_EXTENSIONS | EBOOK_EXTENSIONS
MAX_MEDIA_CANDIDATES = 20_000


def _regular_file_stat(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info


def discover_media_candidates(path: str, *, limit: int = MAX_MEDIA_CANDIDATES) -> list[str]:
    """Return deterministic audiobook/ebook candidate files without following symlinks."""
    root = Path(path)
    if root.is_file():
        return [str(root)] if root.suffix.lower() in MEDIA_CANDIDATE_EXTENSIONS else []
    if not root.is_dir():
        return []

    found: list[str] = []
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(directory_names, key=str.casefold)
        for name in sorted(file_names, key=str.casefold):
            candidate = Path(current_root) / name
            if candidate.suffix.lower() not in MEDIA_CANDIDATE_EXTENSIONS:
                continue
            if _regular_file_stat(candidate) is None:
                continue
            found.append(str(candidate))
            if len(found) >= max(1, int(limit)):
                return found
    return found


def _read_head(path: Path, size: int = 96) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("O_NOFOLLOW is unavailable")
    fd = os.open(path, flags | nofollow)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("media candidate is not a regular file")
        return os.read(fd, size)
    finally:
        os.close(fd)


def _epub_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            if "mimetype" not in archive.namelist():
                return False
            value = archive.read("mimetype")
            return value.removeprefix(b"\xef\xbb\xbf").strip() == b"application/epub+zip"
    except (OSError, RuntimeError, KeyError, zipfile.BadZipFile):
        return False


def detect_file_media_kind(
    path: str,
    *,
    audio_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Identify the actual media family from bytes/ffprobe instead of trusting the name."""
    target = Path(path)
    try:
        info = target.lstat()
    except OSError as exc:
        return {
            "path": path,
            "kind": "missing",
            "detectedFormat": "missing",
            "confidence": 100,
            "source": "filesystem",
            "error": str(exc)[:300],
        }

    if stat.S_ISLNK(info.st_mode):
        return {
            "path": path,
            "kind": "unsafe",
            "detectedFormat": "symlink",
            "confidence": 100,
            "source": "filesystem",
        }
    if not stat.S_ISREG(info.st_mode):
        return {
            "path": path,
            "kind": "unknown",
            "detectedFormat": "non_regular",
            "confidence": 100,
            "source": "filesystem",
        }

    try:
        head = _read_head(target)
    except OSError as exc:
        return {
            "path": path,
            "kind": "unknown",
            "detectedFormat": "unreadable",
            "confidence": 0,
            "source": "filesystem",
            "error": str(exc)[:300],
        }

    if head.startswith(b"%PDF-"):
        return {
            "path": path,
            "kind": "ebook",
            "detectedFormat": "pdf",
            "confidence": 100,
            "source": "signature",
        }
    if len(head) >= 68 and head[60:68] == b"BOOKMOBI":
        return {
            "path": path,
            "kind": "ebook",
            "detectedFormat": "mobi",
            "confidence": 100,
            "source": "signature",
        }
    if head.lstrip(b"\xef\xbb\xbf\t\r\n ").startswith(b"{\\rtf"):
        return {
            "path": path,
            "kind": "ebook",
            "detectedFormat": "rtf",
            "confidence": 100,
            "source": "signature",
        }
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")) and _epub_zip(target):
        return {
            "path": path,
            "kind": "ebook",
            "detectedFormat": "epub",
            "confidence": 100,
            "source": "signature+epub-mimetype",
        }

    probe = audio_probe if audio_probe is not None else probe_audio_file(path)
    if not probe.get("probe_error") and int(probe.get("audio_stream_count") or 0) > 0:
        return {
            "path": path,
            "kind": "audiobook",
            "detectedFormat": str(probe.get("format_name") or "audio"),
            "codec": str(probe.get("codec") or ""),
            "durationSeconds": probe.get("duration_seconds"),
            "confidence": 100,
            "source": "ffprobe",
        }

    return {
        "path": path,
        "kind": "unknown",
        "detectedFormat": "unknown",
        "confidence": 0,
        "source": "signature+ffprobe",
        "probeError": str(probe.get("probe_error") or ""),
    }


def media_set_fingerprint(path: str) -> str:
    """Fingerprint the current set of candidate files using immutable filesystem identity fields."""
    root = Path(path)
    candidates = discover_media_candidates(path)
    entries: list[dict[str, Any]] = []

    if root.exists():
        root_info = root.lstat()
        entries.append({
            "path": ".",
            "mode": stat.S_IFMT(root_info.st_mode),
            "device": int(root_info.st_dev),
            "inode": int(root_info.st_ino),
            "size": int(root_info.st_size),
            "mtimeNs": int(root_info.st_mtime_ns),
            "ctimeNs": int(root_info.st_ctime_ns),
        })

    for candidate in candidates:
        target = Path(candidate)
        info = target.lstat()
        try:
            relative = target.relative_to(root).as_posix() if root.is_dir() else target.name
        except ValueError:
            relative = str(target)
        entries.append({
            "path": relative,
            "mode": stat.S_IFMT(info.st_mode),
            "device": int(info.st_dev),
            "inode": int(info.st_ino),
            "size": int(info.st_size),
            "mtimeNs": int(info.st_mtime_ns),
            "ctimeNs": int(info.st_ctime_ns),
        })

    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "media-set:" + hashlib.sha256(raw).hexdigest()


def inspect_media_path(path: str) -> dict[str, Any]:
    """Build a bounded read-only actual-media inventory for one Bindery path."""
    candidates = discover_media_candidates(path)
    items = [detect_file_media_kind(candidate) for candidate in candidates]
    counts: dict[str, int] = {}
    for item in items:
        kind = str(item.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "path": path,
        "candidateCount": len(candidates),
        "counts": counts,
        "items": items,
    }
