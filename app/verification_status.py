"""Tell a malware scan that produced no verdict apart from evidence against a file.

A scanner timeout, dropped connection, clamd error, or size limit means the scan
could not finish. That must still block admission (nothing unscanned is let in),
but it is not evidence that a file is unsafe, so it must never drive quarantine
and must not be served from the verification cache as if it were a finding.
"""

from __future__ import annotations

from typing import Any

# scan_with_clamd produced these messages before scanner failures carried an
# explicit "inconclusive" flag. Verdicts saved by earlier releases only have the
# message, so they are recognised by it.
_LEGACY_SCANNER_FAILURE_PREFIXES = (
    "ClamAV malware scan failed:",
    "ClamAV reported an error:",
    "Unexpected ClamAV response:",
    "ClamAV malware scanning is enabled but no clamd host is configured.",
    "Unable to stat the file for malware scanning:",
)
_LEGACY_SIZE_LIMIT_FRAGMENT = "above the configured malware scan limit"


def malware_scan_inconclusive(security: Any) -> bool:
    """True when the only failed safety check is a malware scan without a verdict."""
    if not isinstance(security, dict):
        return False
    checks = security.get("checks")
    if not isinstance(checks, dict):
        return False
    failed = [
        name
        for name, check in checks.items()
        if isinstance(check, dict) and check.get("status") == "failed"
    ]
    if failed != ["malwareScan"]:
        return False
    check = checks["malwareScan"]
    if "inconclusive" in check:
        return check.get("inconclusive") is True
    message = str(check.get("message") or "")
    return (
        message.startswith(_LEGACY_SCANNER_FAILURE_PREFIXES)
        or _LEGACY_SIZE_LIMIT_FRAGMENT in message
    )


def verification_is_inconclusive(verdict: Any, evidence: Any) -> bool:
    """True for a saved verdict that reflects a scanner outage, not the file.

    Current releases save such a result as INSUFFICIENT_EVIDENCE with
    ``malwareScanInconclusive``; earlier releases saved it as UNSAFE_FILE.
    """
    if not isinstance(evidence, dict):
        return False
    if evidence.get("malwareScanInconclusive") is True:
        return True
    if str(verdict or "").upper() == "UNSAFE_FILE":
        return malware_scan_inconclusive(evidence.get("security"))
    return False
