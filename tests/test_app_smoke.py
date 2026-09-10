import app.main as main
from app.config import Settings


def test_app_imports():
    assert main.app.title == "BookGuard"
    assert main.app.version == "0.4.4"


def test_settings_clamp_and_lists():
    cfg = Settings()
    cfg.apply({
        "sample_files": 999,
        "dashboard_poll_ms": 10,
        "title_min_shared_words": 3,
        "strong_mismatch_consensus_percent": 10,
        "music_genres": "Rock\nAmbient\nrock\n",
        "author_aliases": "Stephen King = Richard Bachman\nStephen King = Richard Bachman\n",
        "metadata_repair_mode": "safe",
        "repair_audio_genre": True,
        "repair_audio_genre_value": "Audiobook",
    })
    assert cfg.sample_files == 50
    assert cfg.dashboard_poll_ms == 500
    assert cfg.title_min_shared_words == 3
    assert cfg.strong_mismatch_consensus_percent == 50
    assert cfg.music_genres == ["Rock", "Ambient"]
    assert cfg.author_aliases == ["Stephen King = Richard Bachman"]
    assert cfg.metadata_repair_mode == "safe"
    assert cfg.repair_audio_genre is True
    assert cfg.repair_audio_genre_value == "Audiobook"


def test_invalid_repair_mode_is_ignored():
    cfg = Settings()
    original = cfg.metadata_repair_mode
    cfg.apply({"metadata_repair_mode": "dangerous"})
    assert cfg.metadata_repair_mode == original


def test_repairs_page_excludes_full_preview_noop(monkeypatch):
    row = {
        "id": 1,
        "risk_score": 5,
        "classification": "PASS",
        "reason_code": "MATCH",
        "author": "Stephen King",
        "title": "Full Dark, No Stars",
        "format": "audiobook",
        "reasons": ["Match"],
        "stored_path": "/data/media/audiobooks/Stephen King/Full Dark, No Stars",
        "local_path": "/audiobooks/Stephen King/Full Dark, No Stars",
        "metadata": {
            "detected_title": "Full Dark, No Stars",
            "detected_author": "Stephen King",
            "detected_genre": "Audiobook",
        },
    }

    monkeypatch.setattr(main.settings, "metadata_repair_mode", "preview")
    monkeypatch.setattr(main, "latest_results", lambda limit=10000: [row])
    monkeypatch.setattr(
        main,
        "repair_candidate_summary",
        lambda result: {
            "eligible": True,
            "safe": True,
            "kind": "AUDIO_TAGS",
            "reason": "Potential scan-level repair.",
        },
    )
    monkeypatch.setattr(
        main,
        "build_repair_preview",
        lambda result: {
            "eligible": False,
            "safe": False,
            "kind": "AUDIO_TAGS",
            "reason": "0 of 1 audio file(s) would change.",
            "before": {},
            "after": {},
        },
    )

    assert main._latest_repair_candidates() == []
