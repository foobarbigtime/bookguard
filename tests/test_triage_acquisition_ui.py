from pathlib import Path


TEMPLATE = Path("templates/triage.html").read_text(encoding="utf-8")
CONTROLLER = Path("static/triage-acquisition.js").read_text(encoding="utf-8")


def test_triage_loads_supervised_replacement_controller():
    assert 'id="acquisitionPanel"' in TEMPLATE
    assert 'id="replacementPanel"' in TEMPLATE
    assert 'data-book-id="{{ row.book_id }}"' in TEMPLATE
    assert 'data-format="{{ row.format }}"' in TEMPLATE
    assert 'data-reason-code="{{ row.reason_code }}"' in TEMPLATE
    assert 'src="/static/triage-acquisition.js"' in TEMPLATE
    assert 'data-replacement-result="${id}"' in TEMPLATE


def test_prepared_missing_mismatch_retains_replacement_entry_point():
    assert "verification.source === 'missing'" in TEMPLATE
    assert "cell.dataset.format === 'ebook'" in TEMPLATE
    assert "cell.dataset.reasonCode === 'MISMATCH'" in TEMPLATE
    assert "'Resume replacement'" in TEMPLATE


def test_controller_uses_existing_guarded_lifecycle_endpoints():
    endpoints = {
        "/api/automatic/acquisition-coordinator",
        "/api/automatic/acquisition-readiness",
        "/api/automatic/acquisitions?limit=20",
        "/api/automatic/admissions?limit=20",
        "/wrong-content-preview",
        "/remediate-wrong-content",
        "/replacement-preview",
        "/acquisitions",
        "/reconcile",
        "/admit",
        "/finalize",
    }

    for endpoint in endpoints:
        assert endpoint in CONTROLLER


def test_controller_preserves_explicit_confirmation_contracts():
    confirmations = {
        "REMEDIATE_WRONG_CONTENT",
        "START_EBOOK_ACQUISITION",
        "RECONCILE_EBOOK_ACQUISITION",
        "ADMIT_EBOOK_ACQUISITION",
        "RECONCILE_ADMISSION",
        "FINALIZE_EBOOK_ACQUISITION",
    }

    for confirmation in confirmations:
        assert confirmation in CONTROLLER


def test_preflight_displays_separate_writable_action_alias():
    assert "Writable action alias" in CONTROLLER
    assert "ebookActionReady" in CONTROLLER
    assert "ebookActionBlockers" in CONTROLLER


def test_controller_does_not_change_safety_settings_or_render_api_html():
    assert "/api/settings" not in CONTROLLER
    assert "BOOKGUARD_" not in CONTROLLER
    assert ".innerHTML" not in CONTROLLER
    assert ".textContent" in CONTROLLER
