from app.media_discovery import (
    discover_audio_files,
    discover_ebook_files,
    representative_items,
    resolve_ebook_target,
)


def test_discovery_is_recursive_and_deterministic(tmp_path):
    root = tmp_path / "library"
    (root / "b").mkdir(parents=True)
    (root / "a").mkdir(parents=True)
    (root / "b" / "track02.mp3").write_bytes(b"two")
    (root / "a" / "track01.flac").write_bytes(b"one")
    (root / "a" / "cover.jpg").write_bytes(b"image")

    discovered = discover_audio_files(str(root))

    assert discovered == sorted(discovered, key=str.casefold)
    assert [path.rsplit("/", 1)[-1] for path in discovered] == [
        "track01.flac",
        "track02.mp3",
    ]


def test_representative_items_preserves_existing_sampling_behavior():
    values = ["01", "02", "03", "04", "05"]

    assert representative_items(values, 3) == ["01", "03", "05"]
    assert representative_items(values, 1) == ["03"]
    assert representative_items(values, 0) == []


def test_resolve_ebook_target_uses_first_supported_file(tmp_path):
    root = tmp_path / "book"
    root.mkdir()
    (root / "notes.jpg").write_bytes(b"ignore")
    (root / "z-last.pdf").write_bytes(b"pdf")
    (root / "a-first.epub").write_bytes(b"epub")

    assert resolve_ebook_target(str(root)).endswith("a-first.epub")
    assert len(discover_ebook_files(str(root))) == 2


def test_resolve_ebook_target_preserves_unresolved_path(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()

    assert resolve_ebook_target(str(root)) == str(root)
    missing = tmp_path / "missing.epub"
    assert resolve_ebook_target(str(missing)) == str(missing)


def test_discovery_callback_keeps_long_walks_cooperative(tmp_path):
    root = tmp_path / "library"
    (root / "one" / "two").mkdir(parents=True)
    (root / "one" / "two" / "book.epub").write_bytes(b"book")
    calls = []

    discovered = discover_ebook_files(str(root), check=lambda: calls.append(True))

    assert discovered
    assert calls
