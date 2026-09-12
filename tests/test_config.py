import pytest

from app.config import (
    DEFAULT_MAX_STAGED_EBOOK_BYTES,
    ConfigurationError,
    load_auth_settings,
    load_automation_settings,
)


def test_automation_settings_are_loaded_on_demand(monkeypatch):
    monkeypatch.setenv("BOOKGUARD_STAGING_ROOT", "/test-staging")
    monkeypatch.setenv("BOOKGUARD_BINDERY_DROP_FOLDER", "/test-drop")
    monkeypatch.setenv("BOOKGUARD_AUTOMATIC_REACQUISITION", "true")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ENABLED", "true")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_ROOT", "/test-admission")
    monkeypatch.setenv("BOOKGUARD_ADMISSION_BINDERY_ROOT", "/bindery-books")
    monkeypatch.delenv("BOOKGUARD_MAX_STAGED_EBOOK_BYTES", raising=False)

    configured = load_automation_settings()

    assert configured.staging_root == "/test-staging"
    assert configured.bindery_drop_folder == "/test-drop"
    assert configured.automatic_reacquisition is True
    assert configured.max_staged_ebook_bytes == DEFAULT_MAX_STAGED_EBOOK_BYTES
    assert configured.admission_enabled is True
    assert configured.admission_root == "/test-admission"
    assert configured.admission_bindery_root == "/bindery-books"


@pytest.mark.parametrize("value", ["not-a-number", "0", "-1"])
def test_invalid_staging_size_limit_fails_closed(monkeypatch, value):
    monkeypatch.setenv("BOOKGUARD_MAX_STAGED_EBOOK_BYTES", value)

    with pytest.raises(ConfigurationError):
        load_automation_settings()


def test_auth_settings_require_a_password(monkeypatch):
    monkeypatch.delenv("BOOKGUARD_AUTH_PASSWORD", raising=False)

    with pytest.raises(ConfigurationError, match="AUTH_PASSWORD is required"):
        load_auth_settings()


def test_auth_username_cannot_contain_basic_auth_separator(monkeypatch):
    monkeypatch.setenv("BOOKGUARD_AUTH_USERNAME", "bad:name")
    monkeypatch.setenv("BOOKGUARD_AUTH_PASSWORD", "secret")

    with pytest.raises(ConfigurationError, match="must not contain a colon"):
        load_auth_settings()
