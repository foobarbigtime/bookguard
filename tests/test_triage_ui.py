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


def _run_complete_summary(job):
    import json, re, shutil, subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    match = re.search(r"function verificationCompleteSummary\(job\) \{.*?\n\}\n", TEMPLATE, re.S)
    assert match, "verificationCompleteSummary not found in triage.html"
    script = match.group(0) + f"process.stdout.write(JSON.stringify(verificationCompleteSummary({json.dumps(job)})));"
    return json.loads(subprocess.run([node, "-e", script], check=True, capture_output=True, text=True).stdout)


def test_completion_summary_does_not_count_postponed_books_as_verified():
    summary = _run_complete_summary({
        "processed": 3, "postponed": 1, "postponedReason": "Bindery series data is unavailable.",
        "counts": {"VERIFIED_CORRECT": 2},
    })

    assert summary["status"] == "Complete: 2 verified, 1 postponed"
    assert "Verified correct: 2" in summary["message"]
    assert "Postponed: 1 (Bindery series data is unavailable.)" in summary["message"]


def test_completion_summary_without_postponements_is_unchanged():
    summary = _run_complete_summary({"processed": 2, "counts": {"VERIFIED_CORRECT": 2}})

    assert summary["status"] == "Complete: 2 verified"
    assert "Postponed" not in summary["message"]
    assert summary["message"].startswith("Content verification complete.\n\nVerified correct: 2")
