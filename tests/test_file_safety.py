from pathlib import Path

from app.file_safety import roots_overlap


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
