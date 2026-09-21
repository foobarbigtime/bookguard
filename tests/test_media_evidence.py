from __future__ import annotations

import os
from pathlib import Path

import app.media_evidence as media_evidence


def test_pdf_bytes_are_detected_as_ebook_even_with_audio_extension(tmp_path, monkeypatch):
    path = tmp_path / "wrong.mp3"
    path.write_bytes(b"%PDF-1.7\nfixture")

    def should_not_probe(_path):
        raise AssertionError("Known ebook signature should not need ffprobe")

    monkeypatch.setattr(
        media_evidence,
        "cached_or_probe_audio_file",
        lambda path: (should_not_probe(path), False),
    )

    result = media_evidence.detect_file_media_kind(str(path))

    assert result["kind"] == "ebook"
    assert result["detectedFormat"] == "pdf"
    assert result["source"] == "signature"


def test_audio_container_is_detected_by_ffprobe_even_with_ebook_extension(tmp_path, monkeypatch):
    path = tmp_path / "wrong.epub"
    path.write_bytes(b"not-an-ebook")

    monkeypatch.setattr(
        media_evidence,
        "cached_or_probe_audio_file",
        lambda _path: (
            {
                "audio_stream_count": 1,
                "format_name": "mp3",
                "codec": "mp3",
                "duration_seconds": 3600.0,
            },
            False,
        ),
    )

    result = media_evidence.detect_file_media_kind(str(path))

    assert result["kind"] == "audiobook"
    assert result["detectedFormat"] == "mp3"
    assert result["confidence"] == 100


def test_media_set_fingerprint_changes_when_candidate_is_replaced(tmp_path):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"AAAA")
    first = media_evidence.media_set_fingerprint(str(tmp_path))

    replacement = tmp_path / "replacement.mp3"
    replacement.write_bytes(b"BBBB")
    old = path.stat()
    os.utime(replacement, ns=(replacement.stat().st_atime_ns, old.st_mtime_ns))
    os.replace(replacement, path)

    second = media_evidence.media_set_fingerprint(str(tmp_path))

    assert first != second


def test_discovery_ignores_cover_art_but_includes_ebook_and_audio_candidates(tmp_path):
    (tmp_path / "book.m4b").write_bytes(b"audio")
    (tmp_path / "book.epub").write_bytes(b"ebook")
    (tmp_path / "cover.jpg").write_bytes(b"image")

    found = media_evidence.discover_media_candidates(str(tmp_path))

    assert {Path(value).name for value in found} == {"book.m4b", "book.epub"}



def test_media_detection_uses_cached_probe_path(tmp_path, monkeypatch):
    path = tmp_path / "track.mp3"
    path.write_bytes(b"audio")
    calls = []

    def cached(path_value):
        calls.append(path_value)
        return (
            {
                "audio_stream_count": 1,
                "format_name": "mp3",
                "codec": "mp3",
                "duration_seconds": 60.0,
            },
            True,
        )

    monkeypatch.setattr(media_evidence, "cached_or_probe_audio_file", cached)

    result = media_evidence.detect_file_media_kind(str(path))

    assert result["kind"] == "audiobook"
    assert calls == [str(path)]
