from pathlib import Path

import app.preimport as preimport


class FakeClient:
    def __init__(self, settings_map):
        self.settings_map = settings_map

    def get_setting(self, key):
        return {"key": key, "value": self.settings_map.get(key, "")}


def test_preimport_readiness_requires_external_mode_and_confirmed_mapping(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()

    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/handoff")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    monkeypatch.setattr(preimport.settings, "ebook_root", str(tmp_path / "books"))
    monkeypatch.setattr(preimport.settings, "audiobook_root", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(preimport.settings, "quarantine_root", str(tmp_path / "quarantine"))

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
    staging = tmp_path / "staging"
    staging.mkdir()

    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/handoff")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    monkeypatch.setattr(preimport.settings, "ebook_root", str(tmp_path / "books"))
    monkeypatch.setattr(preimport.settings, "audiobook_root", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(preimport.settings, "quarantine_root", str(tmp_path / "quarantine"))

    client = FakeClient({
        "import.mode": "copy",
        "import.drop_folder": "/handoff",
    })

    result = preimport.preimport_readiness(client)
    assert result["ready"] is False
    assert result["automaticGrabAllowed"] is False
    assert "binderyExternalImport" in result["blockers"]


def test_preimport_readiness_requires_explicit_drop_mapping_confirmation(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()

    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", str(staging))
    monkeypatch.delenv("BOOKGUARD_BINDERY_DROP_FOLDER", raising=False)
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    monkeypatch.setattr(preimport.settings, "ebook_root", str(tmp_path / "books"))
    monkeypatch.setattr(preimport.settings, "audiobook_root", str(tmp_path / "audiobooks"))
    monkeypatch.setattr(preimport.settings, "quarantine_root", str(tmp_path / "quarantine"))

    client = FakeClient({
        "import.mode": "external",
        "import.drop_folder": "/handoff",
    })

    result = preimport.preimport_readiness(client)
    assert result["ready"] is False
    assert "dropFolderMappingConfirmed" in result["blockers"]
