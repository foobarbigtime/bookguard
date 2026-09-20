from types import SimpleNamespace

import app.diagnostics as diagnostics


def test_explain_blockers_returns_operator_guidance():
    items = diagnostics.explain_blockers(
        ["binderyExternalImport", "stagingRootWritable", "unknownFutureGate"]
    )

    assert items[0]["label"] == "Bindery is not using external import mode"
    assert "external" in items[0]["fix"]
    assert items[1]["label"] == "BookGuard staging root is not writable"
    assert items[2]["key"] == "unknownFutureGate"
    assert items[2]["why"] == "This safety check did not pass."


def test_diagnostics_snapshot_is_secret_free(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "preimport_readiness",
        lambda: {
            "ready": False,
            "blockers": ["binderyExternalImport"],
            "message": "blocked",
            "checks": {"binderyExternalImport": False},
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "acquisition_readiness",
        lambda: {
            "ready": False,
            "blockers": ["actionsEnabled"],
            "message": "blocked",
            "checks": {"actionsEnabled": False},
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "admission_readiness",
        lambda: {
            "ready": True,
            "blockers": [],
            "message": "ready",
            "checks": {},
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "acquisition_coordinator_status",
        lambda: {
            "enabled": False,
            "running": False,
            "state": "disabled",
            "blockers": ["coordinatorEnabled"],
            "lastError": None,
            "lastRunAt": None,
            "lastSuccessAt": None,
            "action": None,
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "load_automation_settings",
        lambda: SimpleNamespace(
            automatic_reacquisition=False,
            acquisition_coordinator_enabled=False,
            admission_enabled=False,
            ebook_actions_enabled=False,
        ),
    )
    monkeypatch.setattr(diagnostics.settings, "allow_actions", False)
    monkeypatch.setattr(diagnostics.settings, "verification_malware_scan", True)
    monkeypatch.setattr(diagnostics.settings, "verification_clamd_host", "clamav")

    result = diagnostics.diagnostics_snapshot()

    assert result["secretsIncluded"] is False
    assert result["gates"]["malwareScanningEnabled"] is True
    assert result["gates"]["malwareScannerConfigured"] is True
    assert result["gateCards"][0]["label"] == "Bindery actions"
    assert result["gateCards"][-1]["label"] == "Malware scanner configured"
    assert result["sections"][0]["explanations"][0]["key"] == "binderyExternalImport"
    assert result["sections"][2]["ready"] is True
