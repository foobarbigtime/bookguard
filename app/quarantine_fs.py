from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import stat

from .file_safety import sha256_file
from .no_replace import rename_no_replace


class QuarantineMoveError(RuntimeError):
    pass


class QuarantineCommitError(RuntimeError):
    def __init__(
        self,
        cause: Exception,
        rollback_error: Exception | None = None,
    ) -> None:
        self.cause = cause
        self.rollback_error = rollback_error
        super().__init__(str(cause))


def _move_no_replace(source: Path, destination: Path) -> None:
    """Move with an atomic destination claim; fail closed on unsupported mounts."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    source_fd = os.open(source.parent, flags)
    try:
        destination_fd = os.open(destination.parent, flags)
        try:
            if rename_no_replace(
                source_fd, source.name, destination_fd, destination.name, destination
            ):
                return
            # A link claims a regular-file destination atomically when renameat2
            # is unavailable. No checked shutil.move or directory rename fallback:
            # both can replace an entry created between check and mutation.
            info = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise QuarantineMoveError("No-replace directory move is unsupported.")
            os.link(
                source.name, destination.name, src_dir_fd=source_fd,
                dst_dir_fd=destination_fd, follow_symlinks=False,
            )
            current = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise QuarantineMoveError("Source changed after destination claim.")
            os.unlink(source.name, dir_fd=source_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def _verify_source_restored(
    observed_source: Path,
    *,
    expected_sha256: str | None,
) -> None:
    if expected_sha256 is None:
        if not observed_source.exists():
            raise QuarantineMoveError(
                "The original source did not reappear after rollback."
            )
        return

    if not observed_source.is_file():
        raise QuarantineMoveError(
            "The original source did not reappear as a file after rollback."
        )
    if sha256_file(observed_source) != expected_sha256:
        raise QuarantineMoveError(
            "The original source checksum changed during rollback."
        )


def rollback_quarantine_move(
    mutation_source: Path,
    observed_source: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> None:
    """Restore one quarantine move and verify the original source again."""
    mutation_source.parent.mkdir(parents=True, exist_ok=True)

    if os.path.lexists(destination) and os.path.lexists(mutation_source):
        raise QuarantineMoveError(
            "Both the quarantine destination and original mutation path exist; "
            "rollback state is ambiguous."
        )
    if os.path.lexists(destination):
        _move_no_replace(destination, mutation_source)

    _verify_source_restored(
        observed_source,
        expected_sha256=expected_sha256,
    )


def move_to_quarantine(
    mutation_source: Path,
    observed_source: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> None:
    """Move one source to quarantine and verify the post-move filesystem state."""
    try:
        _move_no_replace(mutation_source, destination)
    except Exception as exc:
        raise QuarantineMoveError(str(exc)) from exc

    try:
        if observed_source.exists():
            raise QuarantineMoveError(
                "The original source remained visible after the quarantine move."
            )
        if expected_sha256 is None:
            if not destination.exists():
                raise QuarantineMoveError(
                    "The quarantine destination does not exist after the move."
                )
        else:
            if not destination.is_file():
                raise QuarantineMoveError(
                    "The quarantine destination is not a file after the move."
                )
            if sha256_file(destination) != expected_sha256:
                raise QuarantineMoveError(
                    "Quarantined file checksum verification failed."
                )
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            rollback_quarantine_move(
                mutation_source,
                observed_source,
                destination,
                expected_sha256=expected_sha256,
            )
        except Exception as rollback_exc:
            rollback_error = rollback_exc

        message = str(exc)
        if rollback_error is not None:
            message += f" Rollback also failed: {rollback_error}"
        raise QuarantineMoveError(message) from exc


def commit_quarantine_or_rollback(
    commit: Callable[[], None],
    mutation_source: Path,
    observed_source: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> None:
    """Run the external commit after a verified move; restore bytes on failure."""
    try:
        commit()
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            rollback_quarantine_move(
                mutation_source,
                observed_source,
                destination,
                expected_sha256=expected_sha256,
            )
        except Exception as rollback_exc:
            rollback_error = rollback_exc
        raise QuarantineCommitError(exc, rollback_error) from exc
