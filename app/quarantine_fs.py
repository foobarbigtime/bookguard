from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import shutil

from .file_safety import sha256_file


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

    if destination.exists() and mutation_source.exists():
        raise QuarantineMoveError(
            "Both the quarantine destination and original mutation path exist; "
            "rollback state is ambiguous."
        )
    if destination.exists():
        shutil.move(str(destination), str(mutation_source))

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
        shutil.move(str(mutation_source), str(destination))
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
