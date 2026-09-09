from app.main import app
from app.config import Settings


def test_app_imports():
    assert app.title == "BookGuard"
    assert app.version == "0.2.0"


def test_settings_clamp_and_music_list():
    cfg = Settings()
    cfg.apply({
        "sample_files": 999,
        "dashboard_poll_ms": 10,
        "title_min_shared_words": 3,
        "music_genres": "Rock\nAmbient\nrock\n",
    })
    assert cfg.sample_files == 50
    assert cfg.dashboard_poll_ms == 500
    assert cfg.title_min_shared_words == 3
    assert cfg.music_genres == ["Rock", "Ambient"]
