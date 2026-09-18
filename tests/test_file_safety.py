import pytest

from app.file_safety import allocate_unique_destination, roots_overlap


def test_roots_overlap_detects_nested_roots_both_directions(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "child"

    assert roots_overlap(parent, child) is True
    assert roots_overlap(child, parent) is True


def test_roots_overlap_detects_equal_roots(tmp_path):
    root = tmp_path / "root"

    assert roots_overlap(root, root) is True


def test_roots_overlap_accepts_separate_roots(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"

    assert roots_overlap(left, right) is False


def test_allocate_unique_destination_creates_directory_and_uses_source_name(tmp_path):
    source = tmp_path / "source" / "Book.epub"
    destination_dir = tmp_path / "quarantine"

    destination = allocate_unique_destination(destination_dir, source)

    assert destination_dir.is_dir()
    assert destination == destination_dir / "Book.epub"
    assert destination.exists() is False


def test_allocate_unique_destination_uses_next_available_suffix(tmp_path):
    source = tmp_path / "Book.epub"
    destination_dir = tmp_path / "quarantine"
    destination_dir.mkdir()
    (destination_dir / "Book.epub").write_bytes(b"one")
    (destination_dir / "Book-2.epub").write_bytes(b"two")

    destination = allocate_unique_destination(destination_dir, source)

    assert destination == destination_dir / "Book-3.epub"


def test_allocate_unique_destination_fails_closed_when_suffix_space_is_exhausted(tmp_path):
    source = tmp_path / "Book.epub"
    destination_dir = tmp_path / "quarantine"
    destination_dir.mkdir()
    for name in ("Book.epub", "Book-2.epub", "Book-3.epub"):
        (destination_dir / name).write_bytes(b"occupied")

    with pytest.raises(RuntimeError, match="Unable to allocate a unique destination"):
        allocate_unique_destination(destination_dir, source, max_suffix=3)
