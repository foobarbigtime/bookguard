from app.main import app
from app.config import Settings


def test_app_imports():
    assert app.title == "BookGuard"
    assert app.version == "0.4.0"


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
