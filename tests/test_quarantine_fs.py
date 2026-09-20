import pytest

from app.file_safety import sha256_file
from app.quarantine_fs import (
    QuarantineCommitError,
    QuarantineMoveError,
    commit_quarantine_or_rollback,
    move_to_quarantine,
    rollback_quarantine_move,
)


def test_move_to_quarantine_verifies_file_hash(tmp_path):
    source = tmp_path / "Book.epub"
    destination = tmp_path / "quarantine" / "Book.epub"
    destination.parent.mkdir()
    source.write_bytes(b"verified bytes")
    expected_hash = sha256_file(source)

    move_to_quarantine(
        source,
        source,
        destination,
        expected_sha256=expected_hash,
    )

    assert source.exists() is False
    assert destination.is_file()
    assert sha256_file(destination) == expected_hash


def test_commit_failure_rolls_verified_file_back(tmp_path):
    source = tmp_path / "Book.epub"
    destination = tmp_path / "quarantine" / "Book.epub"
    destination.parent.mkdir()
    source.write_bytes(b"verified bytes")
    expected_hash = sha256_file(source)

    move_to_quarantine(
        source,
        source,
        destination,
        expected_sha256=expected_hash,
    )

    def fail_commit():
        raise RuntimeError("Bindery failed")

    with pytest.raises(QuarantineCommitError) as raised:
        commit_quarantine_or_rollback(
            fail_commit,
            source,
            source,
            destination,
            expected_sha256=expected_hash,
        )

    assert str(raised.value.cause) == "Bindery failed"
    assert raised.value.rollback_error is None
    assert source.is_file()
    assert sha256_file(source) == expected_hash
    assert destination.exists() is False


def test_directory_quarantine_supports_manual_path_moves(tmp_path):
    source = tmp_path / "Audiobook"
    destination = tmp_path / "quarantine" / "Audiobook"
    destination.parent.mkdir()
    source.mkdir()
    (source / "track01.mp3").write_bytes(b"audio")

    move_to_quarantine(source, source, destination)

    assert source.exists() is False
    assert (destination / "track01.mp3").is_file()


def test_rollback_fails_closed_when_source_and_destination_both_exist(tmp_path):
    source = tmp_path / "Book.epub"
    destination = tmp_path / "quarantine" / "Book.epub"
    destination.parent.mkdir()
    source.write_bytes(b"source")
    destination.write_bytes(b"quarantine")

    with pytest.raises(QuarantineMoveError, match="rollback state is ambiguous"):
        rollback_quarantine_move(source, source, destination)
