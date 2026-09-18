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
