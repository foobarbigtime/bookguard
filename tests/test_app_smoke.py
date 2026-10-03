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
    ("GET", "/review"),
    ("GET", "/activity"),
    ("GET", "/activity/{kind}/{record_id}"),
    ("GET", "/system"),
    # Redirects from the pages' names before the UI redesign.
    ("GET", "/triage"),
    ("GET", "/history"),
    ("GET", "/history/{kind}/{record_id}"),
    ("GET", "/diagnostics"),
    ("GET", "/settings"),
    ("GET", "/repairs"),
    ("GET", "/health"),
    ("POST", "/api/scan"),
    ("POST", "/api/scan/cancel-safe"),
    ("POST", "/api/scan/stop-immediately"),
    ("GET", "/api/status"),
    ("GET", "/api/diagnostics"),
    ("GET", "/api/history"),
    ("GET", "/api/history/{kind}/{record_id}"),
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
    ("GET", "/api/triage/{result_id}/move-preview"),
    ("POST", "/api/triage/{result_id}/move-to-correct-book"),
    ("POST", "/api/catalogue-moves/{move_id}/reconcile"),
    ("GET", "/api/catalogue-moves"),
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
    ("POST", "/api/automatic/run"),
    ("GET", "/api/automatic/execution-policy"),
    ("GET", "/api/automatic/executions"),
    ("GET", "/api/automatic/observe"),
    ("POST", "/api/automatic/observe/run"),
    ("GET", "/api/automatic/bindery-status"),
    ("GET", "/api/automatic/preimport-readiness"),
    ("GET", "/api/automatic/staging/files"),
    ("POST", "/api/automatic/books/{book_id}/staged-verification"),
    ("GET", "/api/automatic/admission-readiness"),
    ("GET", "/api/automatic/admissions"),
    ("GET", "/api/automatic/admissions/{admission_id}/prepublication-preview"),
    ("POST", "/api/automatic/results/{result_id}/admit-staged-ebook"),
    ("POST", "/api/automatic/admissions/{admission_id}/reconcile"),
    ("POST", "/api/automatic/admissions/{admission_id}/correct-registration"),
    ("GET", "/api/automatic/acquisition-readiness"),
    ("GET", "/api/automatic/acquisitions"),
    ("GET", "/api/automatic/acquisitions/{acquisition_id}/admission-preview"),
    ("GET", "/api/automatic/acquisitions/{acquisition_id}/alternate-preview"),
    ("GET", "/api/automatic/acquisitions/{acquisition_id}/alternate-selection"),
    ("POST", "/api/automatic/plans/{plan_id}/alternate-selection"),
    ("GET", "/api/automatic/plans/{plan_id}/quarantine-replacement-preview"),
    ("GET", "/api/automatic/plans/{plan_id}/quarantine-final-state-preview"),
    ("GET", "/api/automatic/plans/{plan_id}/quarantine-candidate-preview"),
    ("GET", "/api/automatic/results/{result_id}/quarantine-candidate-selection"),
    ("POST", "/api/automatic/plans/{plan_id}/quarantine-candidate-selection"),
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
    assert main.app.version == "0.6.0"


def _api_routes(routes, prefix=""):
    """Yield (path, route) pairs, descending into routers FastAPI includes lazily."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            yield from _api_routes(included.routes, prefix + getattr(context, "prefix", ""))


def test_route_contract_is_exact():
    api_routes = list(_api_routes(main.app.routes))
    routes = {
        (method, path)
        for path, route in api_routes
        for method in route.methods
    }
    route_count = sum(len(route.methods) for _, route in api_routes)

    assert len(routes) == route_count
    assert routes == EXPECTED_ROUTES


def test_templates_parse():
    env = Environment(loader=FileSystemLoader("templates"))
    env.get_template("index.html")
    env.get_template("attention.html")
    env.get_template("history.html")
    env.get_template("history_detail.html")
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
