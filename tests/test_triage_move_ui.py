import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.static_assets import static_url

from app.catalogue_move import CONFIRMATION


TEMPLATE = Path("templates/triage.html").read_text(encoding="utf-8")
CONTROLLER = Path("static/triage-move.js").read_text(encoding="utf-8")
MOVE_MODULE = Path("app/catalogue_move.py").read_text(encoding="utf-8")


def render_triage(moves):
    env = Environment(loader=FileSystemLoader("templates"))
    env.globals["static_url"] = static_url
    counts = {"open": 0, "total": 0, "resolved": 0}
    return env.get_template("triage.html").render(
        classification="REJECT",
        reason_code="",
        show_resolved=False,
        summary={"REVIEW": counts, "REJECT": counts},
        reasons={},
        rows=[],
        allow_actions=True,
        cleanup_history=[],
        catalogue_moves=moves,
        version="test",
    )


def move(move_id, result_id, status):
    return {
        "id": move_id, "result_id": result_id, "status": status,
        "source_book_id": 20, "target_book_id": 21, "target_title": "Kill Alex Cross",
        "source_path": "/data/media/books/Kill ()/Kill.azw3",
        "destination": "/data/media/books/Kill Alex Cross (2011)/Kill Alex Cross.azw3",
        "sha256": "sha256:abc", "detail": {}, "created_at": "t0", "updated_at": "t1",
    }


def test_triage_loads_move_controller_and_panel():
    assert "src=\"{{ static_url('triage-move.js') }}\"" in TEMPLATE
    assert 'id="movePanel"' in TEMPLATE
    assert 'data-move-result="${id}"' in TEMPLATE


def test_move_is_offered_only_for_another_books_missing_ebook():
    assert "relationship === 'missing_ebook'" in TEMPLATE
    assert "verification.verdict === 'WRONG_CONTENT' && relationship === 'missing_ebook'" in TEMPLATE


def test_controller_uses_move_endpoints_and_exact_confirmation():
    assert "/move-preview" in CONTROLLER
    assert "/move-to-correct-book" in CONTROLLER
    assert "/api/catalogue-moves/${moveId}/reconcile" in CONTROLLER
    assert f'confirm: "{CONFIRMATION}"' in CONTROLLER
    assert "window.confirm(" in CONTROLLER


def test_controller_explains_every_named_blocker():
    codes = set(re.findall(r'blockers\.append\("(\w+)"\)', MOVE_MODULE))
    assert codes
    for code in codes:
        assert f"{code}:" in CONTROLLER
    assert 'code.startsWith("binderyPreview:")' in CONTROLLER


def test_controller_never_builds_html_from_server_text():
    assert "innerHTML" not in CONTROLLER


def test_move_history_offers_read_only_recheck_for_unfinished_moves():
    page = render_triage([move(3, 7, "pending"), move(2, 8, "confirmed"), move(1, 9, "request_failed")])
    assert "Moves to the right book" in page
    assert page.count('data-move-reconcile="3"') == 1
    assert 'data-move-reconcile="2"' not in page
    assert 'data-move-reconcile="1"' not in page


def test_move_history_is_hidden_until_a_move_exists():
    assert "Moves to the right book" not in render_triage([])


def test_move_preview_warns_when_the_file_is_not_in_english():
    assert "proof.language.nonEnglish" in CONTROLLER
    assert "If you only keep English books, quarantine it instead." in CONTROLLER
