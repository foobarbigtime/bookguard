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
            staging_root="/staging",
            ebook_action_root="/action-books",
            admission_root="/admission-books",
        ),
    )
    monkeypatch.setattr(diagnostics.settings, "allow_actions", False)
    monkeypatch.setattr(diagnostics.settings, "verification_malware_scan", True)
    monkeypatch.setattr(diagnostics.settings, "verification_clamd_host", "clamav")
    monkeypatch.setattr(
        diagnostics,
        "probe_clamd",
        lambda **kwargs: {
            "ok": True,
            "message": "ClamAV responded to PING.",
            "version": "ClamAV test",
        },
    )

    result = diagnostics.diagnostics_snapshot()

    assert result["secretsIncluded"] is False
    assert result["gates"]["malwareScanningEnabled"] is True
    assert result["gates"]["malwareScannerConfigured"] is True
    assert result["gateCards"][0]["label"] == "Bindery actions"
    assert result["gateCards"][-1]["label"] == "Malware scanner configured"
    assert result["sections"][0]["explanations"][0]["key"] == "binderyExternalImport"
    assert result["sections"][2]["ready"] is True
    assert result["deployment"]["malware"]["reachable"] is True
    assert result["deployment"]["malware"]["version"] == "ClamAV test"
    assert "bindery_api_key" not in str(result).lower()


def test_build_report_uses_safe_provenance_environment(monkeypatch):
    monkeypatch.setenv("BOOKGUARD_BUILD_VERSION", diagnostics.__version__)
    monkeypatch.setenv("BOOKGUARD_BUILD_REVISION", "abc123")
    monkeypatch.setenv(
        "BOOKGUARD_BUILD_SOURCE",
        "https://github.com/foobarbigtime/bookguard",
    )

    report = diagnostics._build_report()

    assert report["applicationVersion"] == diagnostics.__version__
    assert report["imageVersion"] == diagnostics.__version__
    assert report["revision"] == "abc123"
    assert report["provenanceComplete"] is True


def test_path_status_uses_mount_mode_without_writing(monkeypatch):
    monkeypatch.setattr(diagnostics.os.path, "exists", lambda path: True)
    monkeypatch.setattr(
        diagnostics,
        "_mount_info",
        lambda path: {"mountPoint": "/books", "readOnly": True},
    )

    report = diagnostics._path_status(
        "ebooks",
        "Ebook library",
        "/books",
        expected_writable=False,
    )

    assert report["observedMode"] == "read-only"
    assert report["expectationMet"] is True
