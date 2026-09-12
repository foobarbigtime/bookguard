import pytest

from app.config import (
    DEFAULT_MAX_STAGED_EBOOK_BYTES,
    ConfigurationError,
    load_automation_settings,
)


def test_automation_settings_are_loaded_on_demand(monkeypatch):
    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", "/test-staging")
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/test-drop")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    monkeypatch.delenv("BOOKGUARD_MAX_STAGED_EBOOK_BYTES", raising=False)

    configured = load_automation_settings()

    assert configured.staging_root == "/test-staging"
    assert configured.bindery_drop_folder == "/test-drop"
    assert configured.automatic_reacquisition is True
    assert configured.max_staged_ebook_bytes == DEFAULT_MAX_STAGED_EBOOK_BYTES


@pytest.mark.parametrize("value", ["not-a-number", "0", "-1"])
def test_invalid_staging_size_limit_fails_closed(monkeypatch, value):
    monkeypatch.setenv("BOOKGUARD_MAX_STAGED_EBOOK_BYTES", value)

    with pytest.raises(ConfigurationError):
        load_automation_settings()
