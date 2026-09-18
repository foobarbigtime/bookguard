from __future__ import annotations

import hashlib
import os

import pytest

from app import file_snapshot
from app.file_snapshot import (
    SnapshotError,
    assert_snapshot_source_current,
    stable_file_fingerprint,
    verification_snapshot,
)


def test_verification_snapshot_preserves_exact_bytes_and_cleans_up(tmp_path):
    source = tmp_path / "book.epub"
    payload = b"bookguard-stable-bytes" * 100
    source.write_bytes(payload)
    root = tmp_path / "private"

    with verification_snapshot(source, temp_root=root, max_bytes=1024 * 1024) as snapshot:
        assert snapshot.path != source
        assert snapshot.path.suffix == ".epub"
        assert snapshot.path.read_bytes() == payload
        assert snapshot.sha256 == hashlib.sha256(payload).hexdigest()
        assert snapshot.fingerprint == f"sha256:{snapshot.sha256}:{len(payload)}"
        assert_snapshot_source_current(snapshot)
        snapshot_path = snapshot.path

    assert snapshot_path.exists() is False


def test_verification_snapshot_refuses_source_symlink(tmp_path):
    source = tmp_path / "real.epub"
    source.write_bytes(b"content")
    link = tmp_path / "link.epub"
    link.symlink_to(source)

    with pytest.raises(SnapshotError) as caught:
        with verification_snapshot(
            link,
            temp_root=tmp_path / "private",
            max_bytes=1024 * 1024,
        ):
            pass

    assert caught.value.code == "symlink"


def test_verification_snapshot_detects_path_replacement_during_copy(tmp_path, monkeypatch):
    source = tmp_path / "book.epub"
    source.write_bytes(b"original-content")
    original_copy = file_snapshot._copy_open_file

    def replacing_copy(source_fd, destination_fd, *, max_bytes):
        result = original_copy(source_fd, destination_fd, max_bytes=max_bytes)
        replacement = tmp_path / "replacement.epub"
        replacement.write_bytes(b"replacement-content")
        os.replace(replacement, source)
        return result

    monkeypatch.setattr(file_snapshot, "_copy_open_file", replacing_copy)

    with pytest.raises(SnapshotError) as caught:
        with verification_snapshot(
            source,
            temp_root=tmp_path / "private",
            max_bytes=1024 * 1024,
        ):
            pass

    assert caught.value.code == "changed"


def test_verification_snapshot_detects_in_place_mutation_during_copy(tmp_path, monkeypatch):
    source = tmp_path / "book.epub"
    source.write_bytes(b"A" * 4096)
    original_copy = file_snapshot._copy_open_file

    def mutating_copy(source_fd, destination_fd, *, max_bytes):
        result = original_copy(source_fd, destination_fd, max_bytes=max_bytes)
        source.write_bytes(b"B" * 4096)
        return result

    monkeypatch.setattr(file_snapshot, "_copy_open_file", mutating_copy)

    with pytest.raises(SnapshotError) as caught:
        with verification_snapshot(
            source,
            temp_root=tmp_path / "private",
            max_bytes=1024 * 1024,
        ):
            pass

    assert caught.value.code == "changed"


def test_verification_snapshot_rechecks_source_after_parsers(tmp_path):
    source = tmp_path / "book.epub"
    source.write_bytes(b"original")

    with verification_snapshot(
        source,
        temp_root=tmp_path / "private",
        max_bytes=1024 * 1024,
    ) as snapshot:
        replacement = tmp_path / "replacement.epub"
        replacement.write_bytes(b"replacement")
        os.replace(replacement, source)

        with pytest.raises(SnapshotError) as caught:
            assert_snapshot_source_current(snapshot)

    assert caught.value.code == "changed"


def test_stable_fingerprint_hashes_entire_file_and_rejects_oversize(tmp_path):
    source = tmp_path / "book.epub"
    payload = b"prefix" + (b"M" * 200000) + b"suffix"
    source.write_bytes(payload)

    fingerprint = stable_file_fingerprint(source, max_bytes=len(payload))
    assert fingerprint == f"sha256:{hashlib.sha256(payload).hexdigest()}:{len(payload)}"

    with pytest.raises(SnapshotError) as caught:
        stable_file_fingerprint(source, max_bytes=len(payload) - 1)

    assert caught.value.code == "too_large"
