from jinja2 import Environment, FileSystemLoader
from fastapi.routing import APIRoute

import app.main as main
from app.config import Settings
from app.services import dashboard


def test_app_imports():
    assert main.app.title == "BookGuard"
    assert main.app.version == "0.5.0"


def test_routes_are_unique_and_guarded():
    routes = {
        (method, route.path)
        for route in main.app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    route_count = sum(
        len(route.methods)
        for route in main.app.routes
        if isinstance(route, APIRoute)
    )

    assert len(routes) == route_count
    assert ("GET", "/health") in routes
    assert ("GET", "/api/automatic/staging/files") in routes
    assert ("POST", "/api/automatic/books/{book_id}/staged-verification") in routes
    assert ("POST", "/api/triage/{result_id}/detach") in routes
    assert ("POST", "/api/triage/{result_id}/quarantine") in routes

    # These legacy endpoints mutated Bindery/media without body confirmation.
    assert ("POST", "/api/results/{result_id}/detach") not in routes
    assert ("POST", "/api/results/{result_id}/quarantine") not in routes


def test_templates_parse():
    env = Environment(loader=FileSystemLoader("templates"))
    env.get_template("index.html")
    env.get_template("triage.html")
    env.get_template("settings.html")


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
        "verification_enabled": True,
        "verification_use_tika": True,
        "verification_tika_url": "http://tika:9998/",
        "verification_max_text_chars": 1,
        "verification_pdf_pages": 999,
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
    assert cfg.verification_enabled is True
    assert cfg.verification_use_tika is True
    assert cfg.verification_tika_url == "http://tika:9998"
    assert cfg.verification_max_text_chars == 50000
    assert cfg.verification_pdf_pages == 100


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

    monkeypatch.setattr(dashboard.settings, "metadata_repair_mode", "preview")
    monkeypatch.setattr(dashboard, "latest_results", lambda limit=10000: [row])
    monkeypatch.setattr(
        dashboard,
        "repair_candidate_summary",
        lambda result: {
            "eligible": True,
            "safe": True,
            "kind": "AUDIO_TAGS",
            "reason": "Potential scan-level repair.",
        },
    )
    monkeypatch.setattr(
        dashboard,
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

    assert dashboard.latest_repair_candidates() == []
