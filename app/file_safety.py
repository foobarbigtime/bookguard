from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest without loading the file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    """Return whether a resolved path is the root itself or one of its children."""
    return path == root or root in path.parents


def roots_overlap(left: Path, right: Path) -> bool:
    """Return whether either root contains the other, including equality."""
    return is_within(left, right) or is_within(right, left)


def allocate_unique_destination(
    directory: Path,
    source: Path,
    *,
    max_suffix: int = 9999,
) -> Path:
    """Create the destination directory and reserve a non-existing filename choice.

    The caller still performs the move. This function only selects a path that
    does not exist at the time of the safety check.
    """
    directory.mkdir(parents=True, exist_ok=True)

    destination = directory / source.name
    if not destination.exists():
        return destination

    for suffix_number in range(2, max_suffix + 1):
        candidate = directory / f"{source.stem}-{suffix_number}{source.suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError("Unable to allocate a unique destination.")
