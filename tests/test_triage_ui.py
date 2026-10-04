from pathlib import Path


TEMPLATE = Path("templates/triage.html").read_text(encoding="utf-8")
SETTINGS_TEMPLATE = Path("templates/settings.html").read_text(encoding="utf-8")


def test_triage_surfaces_deterministic_security_results():
    assert "Verification and security" in TEMPLATE
    assert "verdict === 'UNSAFE_FILE'" in TEMPLATE
    assert "Unsafe files:" in TEMPLATE
    assert "Deterministic security" in TEMPLATE
    assert 'name="verification_archive_safety"' in SETTINGS_TEMPLATE


def test_triage_no_longer_offers_bookguard_downloads():
    # Replacing a bad file is Replace on Review → Books; Bindery downloads.
    assert "acquisitionPanel" not in TEMPLATE
    assert "replacementPanel" not in TEMPLATE
    assert "triage-acquisition.js" not in TEMPLATE
