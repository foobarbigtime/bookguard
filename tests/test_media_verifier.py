from __future__ import annotations

from app.config import settings
from app.db import init_local_db
import app.verifier as verifier


def _result(*, fmt: str, local_path: str):
    return {
        "id": 1,
        "scan_id": "scan-1",
        "file_id": 10,
        "book_id": 20,
        "classification": "REVIEW",
        "reason_code": "MISMATCH",
        "format": fmt,
        "author": "Example Author",
        "title": "Example Book",
        "stored_path": local_path,
        "local_path": local_path,
        "reasons": ["fixture"],
        "metadata": {},
    }


def test_audiobook_verification_persists_unified_evidence(monkeypatch, tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    verifier.init_verification_db()
    try:
        monkeypatch.setattr(verifier, "media_set_fingerprint", lambda path: "media-set:test")
        monkeypatch.setattr(
            verifier,
            "build_audiobook_evidence",
            lambda result, path: {
                "verdict": "VERIFIED_CORRECT",
                "confidence": 95,
                "source": "audiobook-evidence",
                "evidence": {
                    "expected": {
                        "title": "Example Book",
                        "author": "Example Author",
                        "mediaKind": "audiobook",
                    },
                    "reasonCode": "AUDIOBOOK_IDENTITY_VERIFIED",
                    "explanation": "fixture",
                },
            },
        )

        first = verifier.verify_result(_result(fmt="audiobook", local_path="/audio"), force=True)
        second = verifier.verify_result(_result(fmt="audiobook", local_path="/audio"), force=False)
    finally:
        settings.config_dir = original

    assert first["verdict"] == "VERIFIED_CORRECT"
    assert first["confidence"] == 95
    assert first["source"] == "audiobook-evidence"
    assert first["cached"] is False
    assert second["id"] == first["id"]
    assert second["cached"] is True


def test_ebook_directory_of_audio_is_recorded_as_wrong_media_type(monkeypatch, tmp_path):
    monkeypatch.setattr(verifier, "bindery_series_context", lambda _id: [])
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    verifier.init_verification_db()
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    try:
        monkeypatch.setattr(
            verifier,
            "_ebook_directory_media_mismatch",
            lambda result: {
                "candidateCount": 2,
                "counts": {"audiobook": 2},
                "items": [
                    {"path": "/books/wrong/01.mp3", "kind": "audiobook"},
                    {"path": "/books/wrong/02.mp3", "kind": "audiobook"},
                ],
            },
        )
        monkeypatch.setattr(verifier, "media_set_fingerprint", lambda path: "media-set:audio-dir")

        result = verifier.verify_result(
            _result(fmt="ebook", local_path=str(tracked)),
            force=True,
        )
    finally:
        settings.config_dir = original

    assert result["verdict"] == "WRONG_MEDIA_TYPE"
    assert result["confidence"] == 100
    assert result["source"] == "media-kind"
    assert result["evidence"]["reasonCode"] == "EXPECTED_EBOOK_FOUND_AUDIO"
