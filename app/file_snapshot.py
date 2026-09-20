from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator


COPY_CHUNK_BYTES = 1024 * 1024


class SnapshotError(RuntimeError):
    """A source file could not be captured as one stable regular-file snapshot."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FileSnapshot:
    source_path: str
    path: Path
    sha256: str
    size: int
    source_dev: int
    source_ino: int
    source_mtime_ns: int
    source_ctime_ns: int

    @property
    def fingerprint(self) -> str:
        return f"sha256:{self.sha256}:{self.size}"


def _open_source(path: str | Path) -> tuple[int, os.stat_result]:
    source = os.fspath(path)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SnapshotError(
            "no_nofollow_support",
            "This platform cannot safely open verification sources without following symlinks.",
        )

    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(source, flags)
    except FileNotFoundError as exc:
        raise SnapshotError("missing", "The tracked ebook file no longer exists.") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SnapshotError(
                "symlink",
                "The tracked ebook path is a symbolic link; verification refuses to follow it.",
            ) from exc
        raise SnapshotError(
            "unreadable",
            f"The tracked ebook file could not be opened safely: {str(exc)[:300]}",
        ) from exc

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SnapshotError(
                "non_regular",
                "The tracked ebook target is not a regular file.",
            )
        current = os.stat(source, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise SnapshotError(
                "changed",
                "The tracked ebook path changed while it was being opened.",
            )
    except Exception:
        os.close(fd)
        raise
    return fd, opened


def _stable_fields(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _assert_fd_stable(fd: int, initial: os.stat_result) -> None:
    current = os.fstat(fd)
    if _stable_fields(current) != _stable_fields(initial):
        raise SnapshotError(
            "changed",
            "The ebook file changed while BookGuard was reading it.",
        )


def _assert_path_matches(path: str | Path, initial: os.stat_result) -> None:
    try:
        current = os.stat(os.fspath(path), follow_symlinks=False)
    except FileNotFoundError as exc:
        raise SnapshotError(
            "changed",
            "The tracked ebook path disappeared during verification.",
        ) from exc
    except OSError as exc:
        raise SnapshotError(
            "changed",
            f"The tracked ebook path could not be re-checked safely: {str(exc)[:300]}",
        ) from exc

    if not stat.S_ISREG(current.st_mode) or _stable_fields(current) != _stable_fields(initial):
        raise SnapshotError(
            "changed",
            "The tracked ebook path changed during verification.",
        )


def _copy_open_file(
    source_fd: int,
    destination_fd: int,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    os.lseek(source_fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    copied = 0
    while True:
        chunk = os.read(source_fd, COPY_CHUNK_BYTES)
        if not chunk:
            break
        copied += len(chunk)
        if copied > max_bytes:
            raise SnapshotError(
                "too_large",
                f"The ebook exceeds the verification snapshot limit of {max_bytes} bytes.",
            )
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:
                raise SnapshotError(
                    "snapshot_write",
                    "Unable to write the verification snapshot.",
                )
            view = view[written:]
    return digest.hexdigest(), copied


def stable_file_fingerprint(
    path: str | Path,
    *,
    max_bytes: int,
) -> str:
    """Hash one regular file through a no-follow descriptor and require path stability."""
    fd, initial = _open_source(path)
    try:
        if initial.st_size > max_bytes:
            raise SnapshotError(
                "too_large",
                f"The ebook is {initial.st_size} bytes, above the verification snapshot limit of {max_bytes} bytes.",
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SnapshotError(
                    "too_large",
                    f"The ebook exceeds the verification snapshot limit of {max_bytes} bytes.",
                )
            digest.update(chunk)
        _assert_fd_stable(fd, initial)
        _assert_path_matches(path, initial)
        return f"sha256:{digest.hexdigest()}:{total}"
    finally:
        os.close(fd)


def assert_snapshot_source_current(snapshot: FileSnapshot) -> None:
    """Require the original path to still identify the exact source captured earlier."""
    try:
        current = os.stat(snapshot.source_path, follow_symlinks=False)
    except OSError as exc:
        raise SnapshotError(
            "changed",
            "The tracked ebook path changed after BookGuard captured its verification snapshot.",
        ) from exc

    expected = (
        snapshot.source_dev,
        snapshot.source_ino,
        snapshot.size,
        snapshot.source_mtime_ns,
        snapshot.source_ctime_ns,
    )
    if not stat.S_ISREG(current.st_mode) or _stable_fields(current) != expected:
        raise SnapshotError(
            "changed",
            "The tracked ebook path changed after BookGuard captured its verification snapshot.",
        )


@contextmanager
def verification_snapshot(
    path: str | Path,
    *,
    temp_root: str | Path,
    max_bytes: int,
) -> Iterator[FileSnapshot]:
    """Capture stable bytes once and give every verification parser the same private file."""
    source = os.fspath(path)
    source_fd, initial = _open_source(source)
    snapshot_path: Path | None = None
    destination_fd: int | None = None
    try:
        if initial.st_size > max_bytes:
            raise SnapshotError(
                "too_large",
                f"The ebook is {initial.st_size} bytes, above the verification snapshot limit of {max_bytes} bytes.",
            )

        root = Path(temp_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root_info = root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise SnapshotError(
                "snapshot_root",
                "The private verification snapshot directory is not a normal directory.",
            )

        suffix = Path(source).suffix.lower()
        destination_fd, raw_path = tempfile.mkstemp(
            prefix=".bookguard-verify-",
            suffix=suffix,
            dir=root,
        )
        snapshot_path = Path(raw_path)
        os.fchmod(destination_fd, 0o600)

        digest, copied = _copy_open_file(
            source_fd,
            destination_fd,
            max_bytes=max_bytes,
        )
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None

        _assert_fd_stable(source_fd, initial)
        _assert_path_matches(source, initial)

        snapshot = FileSnapshot(
            source_path=source,
            path=snapshot_path,
            sha256=digest,
            size=copied,
            source_dev=int(initial.st_dev),
            source_ino=int(initial.st_ino),
            source_mtime_ns=int(initial.st_mtime_ns),
            source_ctime_ns=int(initial.st_ctime_ns),
        )
        yield snapshot
    finally:
        if destination_fd is not None:
            try:
                os.close(destination_fd)
            except OSError:
                pass
        try:
            os.close(source_fd)
        except OSError:
            pass
        if snapshot_path is not None:
            try:
                snapshot_path.unlink()
            except FileNotFoundError:
                pass
