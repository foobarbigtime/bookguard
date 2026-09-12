from pathlib import Path

import app.preimport as preimport
from app.bindery_client import BinderyClientError


class FakeClient:
    def __init__(self, settings_map):
        self.settings_map = settings_map

    def get_setting(self, key):
        return {"key": key, "value": self.settings_map.get(key, "")}


class MissingSettingClient:
    def get_setting(self, key):
        raise BinderyClientError(
            f'Bindery GET /setting/{key} returned HTTP 404: {{"error":"setting not found"}}'
        )


def _configure_paths(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.setattr(preimport.settings, "ebook_root", str(tmp_path / "books"))
    monkeypatch.setattr(preimport.settings, "audiobook_root", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(preimport.settings, "quarantine_root", str(tmp_path / "quarantine"))
    return staging


def test_preimport_readiness_requires_external_mode_and_confirmed_mapping(tmp_path, monkeypatch):
    _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/handoff")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")

    client = FakeClient({
        "import.mode": "external",
        "import.drop_folder": "/handoff",
        "import.drop_layout": "flat",
        "import.drop_link_mode": "copy",
    })

    result = preimport.preimport_readiness(client)
    assert result["ready"] is True
    assert result["automaticGrabAllowed"] is True
    assert result["blockers"] == []


def test_preimport_readiness_fails_closed_for_normal_bindery_import(tmp_path, monkeypatch):
    _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/handoff")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")

    client = FakeClient({
        "import.mode": "copy",
        "import.drop_folder": "/handoff",
    })

    result = preimport.preimport_readiness(client)
    assert result["ready"] is False
    assert result["automaticGrabAllowed"] is False
    assert "binderyExternalImport" in result["blockers"]


def test_preimport_readiness_requires_explicit_drop_mapping_confirmation(tmp_path, monkeypatch):
    _configure_paths(tmp_path, monkeypatch)
    monkeypatch.delenv("BOOKGUARD_BINDERY_DROP_FOLDER", raising=False)
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")

    client = FakeClient({
        "import.mode": "external",
        "import.drop_folder": "/handoff",
    })

    result = preimport.preimport_readiness(client)
    assert result["ready"] is False
    assert "dropFolderMappingConfirmed" in result["blockers"]


def test_missing_bindery_settings_use_builtin_defaults_instead_of_error(tmp_path, monkeypatch):
    _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/data/bookguard-staging")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "false")

    result = preimport.preimport_readiness(MissingSettingClient())

    assert result["ready"] is False
    assert result["bindery"]["importMode"] == "auto"
    assert result["bindery"]["dropFolder"] == ""
    assert result["bindery"]["dropLayout"] == "flat"
    assert result["bindery"]["dropLinkMode"] == "copy"


def test_staging_and_quarantine_must_not_contain_each_other(tmp_path, monkeypatch):
    staging = _configure_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(preimport.settings, "quarantine_root", str(staging / "quarantine"))
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/handoff")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")

    client = FakeClient({
        "import.mode": "external",
        "import.drop_folder": "/handoff",
        "import.drop_layout": "flat",
        "import.drop_link_mode": "copy",
    })

    result = preimport.preimport_readiness(client)

    assert result["ready"] is False
    assert result["checks"]["stagingSeparateFromQuarantine"] is False
