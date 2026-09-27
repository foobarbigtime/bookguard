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


def test_move_never_replaces_a_destination_created_after_allocation(tmp_path):
    source = tmp_path / "Book.epub"
    source.write_bytes(b"suspect bytes")
    destination = tmp_path / "quarantine" / "Book.epub"
    destination.parent.mkdir()
    # Simulates another quarantine claiming the name after it was allocated.
    destination.write_bytes(b"earlier quarantined bytes")

    with pytest.raises(QuarantineMoveError):
        move_to_quarantine(source, source, destination)

    assert source.read_bytes() == b"suspect bytes"
    assert destination.read_bytes() == b"earlier quarantined bytes"


def test_directory_move_never_merges_into_existing_destination(tmp_path):
    source = tmp_path / "library" / "Audiobook"
    source.mkdir(parents=True)
    (source / "01.mp3").write_bytes(b"track")
    destination = tmp_path / "quarantine" / "Audiobook"
    destination.mkdir(parents=True)
    (destination / "other.mp3").write_bytes(b"other")

    with pytest.raises(QuarantineMoveError):
        move_to_quarantine(source, source, destination)

    assert (source / "01.mp3").read_bytes() == b"track"
    assert sorted(p.name for p in destination.iterdir()) == ["other.mp3"]


def test_file_move_preserves_inode_and_removes_source(tmp_path):
    source = tmp_path / "Book.epub"
    source.write_bytes(b"bytes")
    inode = source.stat().st_ino
    destination = tmp_path / "quarantine" / "Book.epub"
    destination.parent.mkdir()

    move_to_quarantine(source, source, destination)

    assert not source.exists()
    assert destination.stat().st_ino == inode
    assert destination.stat().st_nlink == 1


def test_file_move_fails_closed_when_no_replace_methods_are_unsupported(tmp_path, monkeypatch):
    import errno
    import app.quarantine_fs as quarantine_fs

    def no_links(*args, **kwargs):
        raise OSError(errno.EPERM, "links not supported")

    monkeypatch.setattr(quarantine_fs.os, "link", no_links)
    monkeypatch.setattr(quarantine_fs, "rename_no_replace", lambda *args: False)
    source = tmp_path / "Book.epub"
    source.write_bytes(b"bytes")
    destination = tmp_path / "quarantine" / "Book.epub"
    destination.parent.mkdir()

    with pytest.raises(QuarantineMoveError):
        move_to_quarantine(source, source, destination)

    assert source.read_bytes() == b"bytes"
    assert not destination.exists()


def test_directory_move_fails_closed_without_atomic_rename(tmp_path, monkeypatch):
    import app.quarantine_fs as quarantine_fs

    monkeypatch.setattr(quarantine_fs, "rename_no_replace", lambda *args: False)
    source = tmp_path / "Book"
    source.mkdir()
    (source / "chapter.txt").write_bytes(b"bytes")
    destination = tmp_path / "quarantine" / "Book"
    destination.parent.mkdir()

    with pytest.raises(QuarantineMoveError, match="unsupported"):
        move_to_quarantine(source, source, destination)

    assert (source / "chapter.txt").read_bytes() == b"bytes"
    assert not destination.exists()
