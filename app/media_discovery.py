from __future__ import annotations

from collections.abc import Callable, Iterable
import os
from pathlib import Path
from typing import TypeVar


AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".flac", ".m4a", ".m4b", ".aac", ".ogg", ".opus", ".wav", ".mp4"}
)
EBOOK_EXTENSIONS = frozenset(
    {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".cbz", ".rtf", ".txt", ".cbr", ".lit"}
)

T = TypeVar("T")
CheckCallback = Callable[[], None]


def discover_media_files(
    path: str,
    extensions: frozenset[str],
    *,
    check: CheckCallback | None = None,
) -> list[str]:
    """Return supported files below path in deterministic order.

    The optional check callback lets long-running callers preserve cooperative
    cancellation without duplicating their own directory-walking logic.
    """
    target = Path(path)
    if check:
        check()
    if target.is_file():
        return [str(target)] if target.suffix.lower() in extensions else []
    if not target.is_dir():
        return []

    found: list[str] = []
    for current_root, _, names in os.walk(target):
        if check:
            check()
        for name in names:
            candidate = Path(current_root) / name
            if candidate.suffix.lower() in extensions and candidate.is_file():
                found.append(str(candidate))
    return sorted(found, key=str.casefold)


def discover_audio_files(path: str, *, check: CheckCallback | None = None) -> list[str]:
    return discover_media_files(path, AUDIO_EXTENSIONS, check=check)


def discover_ebook_files(path: str, *, check: CheckCallback | None = None) -> list[str]:
    return discover_media_files(path, EBOOK_EXTENSIONS, check=check)


def representative_items(items: Iterable[T], limit: int) -> list[T]:
    """Pick deterministic first/middle/last-style samples across an ordered sequence."""
    values = list(items)
    if not values or limit <= 0:
        return []
    if len(values) <= limit:
        return values
    if limit == 1:
        return [values[len(values) // 2]]

    indexes = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    selected: list[T] = []
    seen: set[int] = set()
    for index in indexes:
        if index not in seen:
            seen.add(index)
            selected.append(values[index])
    return selected


def representative_audio_files(path: str, limit: int) -> list[str]:
    return representative_items(discover_audio_files(path), limit)


def resolve_ebook_target(
    local_path: str,
    *,
    check: CheckCallback | None = None,
) -> str:
    """Resolve a tracked ebook path to its first supported file, if it is a directory."""
    target = Path(local_path)
    if check:
        check()
    if target.is_file() or not target.is_dir():
        return local_path

    candidates = discover_ebook_files(local_path, check=check)
    return candidates[0] if candidates else local_path
