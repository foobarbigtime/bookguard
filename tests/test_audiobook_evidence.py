from __future__ import annotations

import app.audiobook_evidence as audiobook_evidence


def _result():
    return {
        "id": 1,
        "scan_id": "scan-1",
        "file_id": 10,
        "book_id": 20,
        "format": "audiobook",
        "title": "The Example Book",
        "author": "Example Author",
        "local_path": "/audiobooks/The Example Book",
    }


def _probe(path: str, **overrides):
    value = {
        "path": path,
        "size_bytes": 1000000,
        "format_name": "mp3",
        "duration_seconds": 3600.0,
        "audio_stream_count": 1,
        "codec": "mp3",
        "sample_rate": 44100,
        "channels": 2,
        "chapter_count": 0,
        "artist": "Example Author",
        "album_artist": "Example Author",
        "author": "Example Author",
        "composer": "",
        "narrator": "",
        "performer": "",
        "album": "The Example Book",
        "title": "Chapter 1",
        "genre": "Audiobook",
        "language": "eng",
        "track": "1",
        "disc": "1",
    }
    value.update(overrides)
    return value


def test_audiobook_evidence_detects_expected_audio_identity(monkeypatch):
    monkeypatch.setattr(
        audiobook_evidence,
        "inspect_media_path",
        lambda path: {
            "candidateCount": 2,
            "counts": {"audiobook": 2},
            "items": [
                {"path": "/audio/01.mp3", "kind": "audiobook", "detectedFormat": "mp3", "confidence": 100, "source": "ffprobe"},
                {"path": "/audio/02.mp3", "kind": "audiobook", "detectedFormat": "mp3", "confidence": 100, "source": "ffprobe"},
            ],
        },
    )
    monkeypatch.setattr(
        audiobook_evidence,
        "verify_audio_files",
        lambda paths: {
            "verdict": "PASS",
            "reason_code": "AUDIO_TECHNICAL_PASS",
            "reasons": ["ok"],
            "file_count": 2,
            "readable_file_count": 2,
            "total_duration_seconds": 7200.0,
            "codecs": ["mp3"],
            "sample_rates": [44100],
            "channels": [2],
            "chapter_count": 0,
            "cache_hits": 0,
            "cache_misses": 2,
            "files": [_probe("/audio/01.mp3"), _probe("/audio/02.mp3", track="2")],
        },
    )

    result = audiobook_evidence.build_audiobook_evidence(_result(), "/audio")

    assert result["verdict"] == "VERIFIED_CORRECT"
    assert result["confidence"] == 95
    assert result["source"] == "audiobook-evidence"
    assert result["evidence"]["identity"]["detected_title"] == "The Example Book"
    assert result["evidence"]["identity"]["detected_author"] == "Example Author"


def test_audiobook_evidence_detects_ebook_instead_of_audio(monkeypatch):
    monkeypatch.setattr(
        audiobook_evidence,
        "inspect_media_path",
        lambda path: {
            "candidateCount": 1,
            "counts": {"ebook": 1},
            "items": [
                {"path": "/audio/wrong.pdf", "kind": "ebook", "detectedFormat": "pdf", "confidence": 100, "source": "signature"},
            ],
        },
    )

    result = audiobook_evidence.build_audiobook_evidence(_result(), "/audio")

    assert result["verdict"] == "WRONG_MEDIA_TYPE"
    assert result["confidence"] == 100
    assert result["evidence"]["reasonCode"] == "EXPECTED_AUDIOBOOK_FOUND_EBOOK"


def test_audiobook_evidence_marks_unreadable_audio_unsafe(monkeypatch):
    monkeypatch.setattr(
        audiobook_evidence,
        "inspect_media_path",
        lambda path: {
            "candidateCount": 1,
            "counts": {"unknown": 1},
            "items": [
                {"path": "/audio/broken.mp3", "kind": "unknown", "detectedFormat": "unknown", "confidence": 0, "source": "signature+ffprobe"},
            ],
        },
    )
    monkeypatch.setattr(
        audiobook_evidence,
        "verify_audio_files",
        lambda paths: {
            "verdict": "FAIL",
            "reason_code": "AUDIO_INTEGRITY_FAILED",
            "reasons": ["broken"],
            "file_count": 1,
            "readable_file_count": 0,
            "total_duration_seconds": 0.0,
            "cache_hits": 0,
            "cache_misses": 1,
            "files": [
                _probe("/audio/broken.mp3", probe_error="invalid data", audio_stream_count=0)
            ],
        },
    )

    result = audiobook_evidence.build_audiobook_evidence(_result(), "/audio")

    assert result["verdict"] == "UNSAFE_FILE"
    assert result["confidence"] == 100
    assert result["source"] == "audiobook-technical"


def test_audiobook_evidence_rejects_consistent_different_identity(monkeypatch):
    monkeypatch.setattr(audiobook_evidence.settings, "reject_strong_mismatch", True)
    monkeypatch.setattr(audiobook_evidence.settings, "strong_mismatch_min_samples", 1)
    monkeypatch.setattr(audiobook_evidence.settings, "strong_mismatch_consensus_percent", 67)
    monkeypatch.setattr(
        audiobook_evidence,
        "inspect_media_path",
        lambda path: {
            "candidateCount": 3,
            "counts": {"audiobook": 3},
            "items": [
                {"path": f"/audio/{i}.mp3", "kind": "audiobook", "detectedFormat": "mp3", "confidence": 100, "source": "ffprobe"}
                for i in range(1, 4)
            ],
        },
    )
    wrong = [
        _probe(
            f"/audio/{i}.mp3",
            album="Different Book",
            author="Different Author",
            artist="Different Author",
            album_artist="Different Author",
        )
        for i in range(1, 4)
    ]
    monkeypatch.setattr(
        audiobook_evidence,
        "verify_audio_files",
        lambda paths: {
            "verdict": "PASS",
            "reason_code": "AUDIO_TECHNICAL_PASS",
            "reasons": ["ok"],
            "file_count": 3,
            "readable_file_count": 3,
            "total_duration_seconds": 10800.0,
            "codecs": ["mp3"],
            "sample_rates": [44100],
            "channels": [2],
            "chapter_count": 0,
            "cache_hits": 0,
            "cache_misses": 3,
            "files": wrong,
        },
    )

    result = audiobook_evidence.build_audiobook_evidence(_result(), "/audio")

    assert result["verdict"] == "WRONG_CONTENT"
    assert result["confidence"] >= 95
