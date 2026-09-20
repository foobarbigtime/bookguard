from jinja2 import Environment, FileSystemLoader
from fastapi.routing import APIRoute

import app.main as main
from app.config import Settings
from app.services import dashboard


EXPECTED_ROUTES = {
    ("GET", "/api/hardlink-conflicts"),
    ("GET", "/api/hardlink-conflicts/{file_id}/preview"),
    ("POST", "/api/hardlink-conflicts/{file_id}/correct"),
    ("POST", "/api/hardlink-conflicts/history/{correction_id}/reconcile"),
    ("GET", "/api/hardlink-conflicts/history/{correction_id}/cleanup-preview"),
    ("POST", "/api/hardlink-conflicts/history/{correction_id}/cleanup"),
    ("POST", "/api/hardlink-conflicts/history/{correction_id}/reconcile-cleanup"),
    ("GET", "/"),
    ("GET", "/attention"),
    ("GET", "/history"),
    ("GET", "/diagnostics"),
    ("GET", "/triage"),
    ("GET", "/settings"),
    ("GET", "/repairs"),
    ("GET", "/health"),
    ("POST", "/api/scan"),
    ("POST", "/api/scan/cancel-safe"),
    ("POST", "/api/scan/stop-immediately"),
    ("GET", "/api/status"),
    ("GET", "/api/diagnostics"),
    ("GET", "/api/history"),
    ("GET", "/api/settings"),
    ("POST", "/api/settings"),
    ("POST", "/api/settings/reset"),
    ("GET", "/api/results/{result_id}/repair-preview"),
    ("POST", "/api/results/{result_id}/repair"),
    ("POST", "/api/repairs/{repair_id}/undo"),
    ("POST", "/api/triage/{result_id}/keep"),
    ("POST", "/api/triage/keep-selected"),
    ("POST", "/api/triage/{result_id}/reopen"),
    ("GET", "/api/triage/{result_id}/action-preview"),
    ("POST", "/api/triage/{result_id}/detach"),
    ("POST", "/api/triage/{result_id}/quarantine"),
    ("GET", "/api/results/{result_id}/missing-detach-preview"),
    ("GET", "/api/missing/preview"),
    ("POST", "/api/results/{result_id}/detach-missing"),
    ("POST", "/api/missing/detach-all"),
    ("GET", "/api/verification/status"),
    ("GET", "/api/verification/cached"),
    ("POST", "/api/verification/start"),
    ("GET", "/api/verification/tika-test"),
    ("GET", "/api/verification/malware-test"),
    ("GET", "/api/verification/{result_id}"),
    ("POST", "/api/verification/{result_id}/run"),
    ("GET", "/api/verification/{result_id}/repair-preview"),
    ("POST", "/api/verification/{result_id}/repair"),
    ("GET", "/api/automatic/bindery-status"),
    ("GET", "/api/automatic/preimport-readiness"),
    ("GET", "/api/automatic/staging/files"),
    ("POST", "/api/automatic/books/{book_id}/staged-verification"),
    ("GET", "/api/automatic/admission-readiness"),
    ("GET", "/api/automatic/admissions"),
    ("POST", "/api/automatic/results/{result_id}/admit-staged-ebook"),
    ("POST", "/api/automatic/admissions/{admission_id}/reconcile"),
    ("POST", "/api/automatic/admissions/{admission_id}/correct-registration"),
    ("GET", "/api/automatic/acquisition-readiness"),
    ("GET", "/api/automatic/acquisitions"),
    ("GET", "/api/automatic/acquisition-coordinator"),
    ("POST", "/api/automatic/results/{result_id}/acquisitions"),
    ("POST", "/api/automatic/acquisitions/{acquisition_id}/reconcile"),
    ("POST", "/api/automatic/acquisitions/{acquisition_id}/admit"),
    ("POST", "/api/automatic/acquisitions/{acquisition_id}/finalize"),
    ("GET", "/api/automatic/books/{book_id}/replacement-preview"),
    ("GET", "/api/automatic/results/{result_id}/wrong-content-preview"),
    ("POST", "/api/automatic/results/{result_id}/remediate-wrong-content"),
}


def test_app_imports():
    assert main.app.title == "BookGuard"
    assert main.app.version == "0.5.0"


def test_route_contract_is_exact():
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
    assert routes == EXPECTED_ROUTES


def test_templates_parse():
    env = Environment(loader=FileSystemLoader("templates"))
    env.get_template("index.html")
    env.get_template("attention.html")
    env.get_template("history.html")
    env.get_template("diagnostics.html")
    env.get_template("triage.html")
    env.get_template("settings.html")
    env.get_template("repairs.html")


def test_settings_clamp_and_lists():
    cfg = Settings(verification_tika_url="http://trusted-tika:9998")
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
        "verification_file_signatures": False,
        "verification_archive_safety": False,
        "verification_epub_structure": False,
        "verification_pdf_integrity": False,
        "verification_malware_scan": True,
        "verification_tika_url": "https://untrusted.example/upload",
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
    assert cfg.verification_file_signatures is False
    assert cfg.verification_archive_safety is False
    assert cfg.verification_epub_structure is False
    assert cfg.verification_pdf_integrity is False
    assert cfg.verification_malware_scan is True
    assert cfg.verification_tika_url == "http://trusted-tika:9998"
    assert cfg.verification_max_text_chars == 50000
    assert cfg.verification_pdf_pages == 100


def test_scan_timing_without_scan():
    assert dashboard.scan_timing(None) == {
        "percent": 0.0,
        "elapsed_seconds": 0,
        "eta_seconds": None,
    }
