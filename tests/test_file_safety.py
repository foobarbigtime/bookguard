import pytest

from app.file_safety import allocate_unique_destination, read_file_prefix, roots_overlap


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



def test_read_file_prefix_does_not_load_bytes_past_limit(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"A" * (1024 * 1024))

    prefix = read_file_prefix(path, max_bytes=4096)

    assert prefix == b"A" * 4096


def test_unlink_exact_file_removes_only_the_expected_inode(tmp_path):
    import pytest

    from app.file_safety import ExactUnlinkError, unlink_exact_file

    root = tmp_path / "staging"
    (root / "sub").mkdir(parents=True)
    target = root / "sub" / "Book.epub"
    target.write_bytes(b"one")
    info = target.stat()

    replacement = root / "sub" / "incoming.tmp"
    replacement.write_bytes(b"replacement")
    keep_old_inode_alive = root / "old-link"
    keep_old_inode_alive.hardlink_to(target)
    replacement.replace(target)  # same name, different inode
    with pytest.raises(ExactUnlinkError):
        unlink_exact_file(root, target, device=info.st_dev, inode=info.st_ino)
    assert target.read_bytes() == b"replacement"

    current = target.stat()
    unlink_exact_file(root, target, device=current.st_dev, inode=current.st_ino)
    assert not target.exists()


def test_unlink_exact_file_refuses_symlinked_parent(tmp_path):
    import pytest

    from app.file_safety import unlink_exact_file

    root = tmp_path / "staging"
    root.mkdir()
    outside = tmp_path / "library"
    outside.mkdir()
    victim = outside / "Book.epub"
    victim.write_bytes(b"library bytes")
    (root / "sub").symlink_to(outside)
    info = victim.stat()

    with pytest.raises(OSError):
        unlink_exact_file(root, root / "sub" / "Book.epub", device=info.st_dev, inode=info.st_ino)
    assert victim.read_bytes() == b"library bytes"
