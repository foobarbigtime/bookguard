from pathlib import Path

import pytest

import app.audiobook_verification as verification_module
from app.audiobook_verification import (
    AudiobookVerificationCancelled,
    discover_audio_files,
    summarize_audiobook_verification,
    verify_audiobook,
)


def _probe(path: str, **overrides):
    value = {
        "path": path,
        "size_bytes": 1_000_000,
        "format_name": "mp3",
        "duration_seconds": 600.0,
        "duration": "600.0",
        "bit_rate": 128000,
        "audio_stream_count": 1,
        "codec": "mp3",
        "sample_rate": 44100,
        "channels": 2,
        "chapter_count": 0,
        "artist": "",
        "album_artist": "",
        "author": "",
        "composer": "",
        "album": "",
        "title": "",
        "genre": "",
        "language": "",
    }
    value.update(overrides)
    return value


def test_discover_audio_files_returns_all_supported_files(tmp_path):
    (tmp_path / "Disc 1").mkdir()
    (tmp_path / "Disc 2").mkdir()
    (tmp_path / "Disc 1" / "01.mp3").write_bytes(b"audio")
    (tmp_path / "Disc 1" / "02.M4B").write_bytes(b"audio")
    (tmp_path / "Disc 2" / "03.flac").write_bytes(b"audio")
    (tmp_path / "cover.jpg").write_bytes(b"image")

    found = discover_audio_files(str(tmp_path))

    assert len(found) == 3
    assert [Path(path).suffix.lower() for path in found] == [".mp3", ".m4b", ".flac"]


def test_technical_verification_passes_consistent_readable_tracks():
    result = summarize_audiobook_verification([
        _probe("01.mp3", duration_seconds=300.0),
        _probe("02.mp3", duration_seconds=450.0),
    ])

    assert result["verdict"] == "PASS"
    assert result["reason_code"] == "AUDIO_TECHNICAL_PASS"
    assert result["file_count"] == 2
    assert result["readable_file_count"] == 2
    assert result["total_duration_seconds"] == 750.0


def test_technical_verification_fails_unreadable_track():
    result = summarize_audiobook_verification([
        _probe("01.mp3"),
        _probe("02.mp3", probe_error="Invalid data found when processing input"),
    ])

    assert result["verdict"] == "FAIL"
    assert result["reason_code"] == "AUDIO_INTEGRITY_FAILED"
    assert result["readable_file_count"] == 1
    assert any("ffprobe could not read" in reason for reason in result["reasons"])


def test_technical_verification_fails_missing_audio_stream():
    result = summarize_audiobook_verification([
        _probe("book.m4b", audio_stream_count=0),
    ])

    assert result["verdict"] == "FAIL"
    assert any("no audio stream" in reason for reason in result["reasons"])


def test_technical_verification_warns_on_track_inconsistency():
    result = summarize_audiobook_verification([
        _probe("01.mp3", sample_rate=44100, channels=2),
        _probe("02.mp3", sample_rate=48000, channels=1),
    ])

    assert result["verdict"] == "REVIEW"
    assert result["reason_code"] == "AUDIO_TECHNICAL_WARNING"
    assert any("multiple sample rates" in reason for reason in result["reasons"])
    assert any("multiple channel" in reason for reason in result["reasons"])


def test_single_m4b_without_chapters_is_warning_not_failure():
    result = summarize_audiobook_verification([
        _probe("book.m4b", format_name="mov,mp4,m4a,3gp,3g2,mj2", codec="aac", chapter_count=0),
    ])

    assert result["verdict"] == "REVIEW"
    assert any("no chapter table" in reason for reason in result["reasons"])


def test_zero_byte_file_is_failure():
    result = summarize_audiobook_verification([
        _probe("broken.mp3", size_bytes=0),
    ])

    assert result["verdict"] == "FAIL"
    assert any("zero bytes" in reason for reason in result["reasons"])


def test_disc_track_sequence_warns_when_only_later_disc_is_present():
    probes = [
        _probe(f"James Patterson - Woman Of God 7-{track:02d}.mp3")
        for track in range(1, 16)
    ]

    result = summarize_audiobook_verification(probes)

    assert result["verdict"] == "REVIEW"
    assert any("starts at disc/part 7" in reason for reason in result["reasons"])


def test_disc_track_sequence_warns_on_missing_track_number():
    result = summarize_audiobook_verification([
        _probe("Book 1-01.mp3"),
        _probe("Book 1-03.mp3"),
    ])

    assert result["verdict"] == "REVIEW"
    assert any("missing track numbers: 2" in reason for reason in result["reasons"])


def test_disc_track_sequence_warns_on_missing_disc_number():
    result = summarize_audiobook_verification([
        _probe("Book 1-01.mp3"),
        _probe("Book 3-01.mp3"),
    ])

    assert result["verdict"] == "REVIEW"
    assert any("missing disc/part numbers: 2" in reason for reason in result["reasons"])


def test_complete_disc_track_sequence_passes():
    result = summarize_audiobook_verification([
        _probe("Book 1-01.mp3"),
        _probe("Book 1-02.mp3"),
        _probe("Book 2-01.mp3"),
        _probe("Book 2-02.mp3"),
    ])

    assert result["verdict"] == "PASS"


def test_unrelated_numeric_filenames_do_not_trigger_sequence_heuristic():
    result = summarize_audiobook_verification([
        _probe("1984 chapter 01.mp3"),
        _probe("1984 chapter 02.mp3"),
    ])

    assert result["verdict"] == "PASS"


def test_verify_audiobook_reports_file_progress(tmp_path, monkeypatch):
    first = tmp_path / "01.mp3"
    second = tmp_path / "02.mp3"
    first.write_bytes(b"audio")
    second.write_bytes(b"audio")

    monkeypatch.setattr(
        verification_module,
        "cached_or_probe_audio_file",
        lambda path, cancel_check=None: (_probe(path, cache_hit=False), False),
    )
    events: list[dict] = []

    result = verify_audiobook(str(tmp_path), progress_callback=events.append)

    assert result["verdict"] == "PASS"
    assert events[0]["phase"] == "discovered"
    assert events[0]["file_total"] == 2
    probing = [event for event in events if event["phase"] == "probing"]
    assert [event["file_index"] for event in probing] == [1, 2]
    assert all(event["file_total"] == 2 for event in probing)
    assert Path(str(probing[0]["path"])).name == "01.mp3"
    assert result["cache_hits"] == 0
    assert result["cache_misses"] == 2
    assert events[-1]["phase"] == "complete"


def test_verify_audiobook_reports_cache_hits(tmp_path, monkeypatch):
    path = tmp_path / "01.mp3"
    path.write_bytes(b"audio")
    monkeypatch.setattr(
        verification_module,
        "cached_or_probe_audio_file",
        lambda path, cancel_check=None: (_probe(path, cache_hit=True), True),
    )
    events: list[dict] = []

    result = verify_audiobook(str(tmp_path), progress_callback=events.append)

    assert result["cache_hits"] == 1
    assert result["cache_misses"] == 0
    assert any(event["phase"] == "cached" for event in events)


def test_verify_audiobook_honors_immediate_cancel_before_probe(tmp_path, monkeypatch):
    path = tmp_path / "01.mp3"
    path.write_bytes(b"audio")
    called = False

    def fake_cached_or_probe(path: str, cancel_check=None):
        nonlocal called
        called = True
        return _probe(path), False

    monkeypatch.setattr(
        verification_module,
        "cached_or_probe_audio_file",
        fake_cached_or_probe,
    )

    with pytest.raises(AudiobookVerificationCancelled):
        verify_audiobook(str(tmp_path), cancel_check=lambda: True)

    assert called is False
