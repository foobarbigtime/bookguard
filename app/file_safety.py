from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def read_file_prefix(path: str | Path, *, max_bytes: int) -> bytes:
    """Read at most max_bytes from the start of a regular file."""
    limit = int(max_bytes)
    if limit < 1:
        raise ValueError("File prefix limit must be positive.")
    with Path(path).open("rb") as handle:
        return handle.read(limit)


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


class ExactUnlinkError(RuntimeError):
    pass


def unlink_exact_file(root: Path, path: Path, *, device: int, inode: int) -> None:
    """Unlink one regular file under ``root`` only if it is still the expected inode.

    The walk from ``root`` uses directory handles with symlink following
    disabled, so neither the parent directories nor the final name can redirect
    the unlink elsewhere, and the name is re-checked against the expected
    ``(device, inode)`` immediately before it is removed.
    """
    root = Path(root)
    relative = Path(path).relative_to(root)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ExactUnlinkError("Refusing to unlink a path that is not a file under the root.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        current = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (device, inode):
            raise ExactUnlinkError("The file changed identity before unlink.")
        if not stat.S_ISREG(current.st_mode):
            raise ExactUnlinkError("Refusing to unlink something that is not a regular file.")
        os.unlink(relative.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
