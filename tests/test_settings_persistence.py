import json

from app.config import settings
from app.db import (
    init_local_db,
    load_persisted_settings,
    local_conn,
    save_persisted_settings,
)


def test_init_purges_legacy_persisted_bindery_api_key(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setattr(settings, "config_dir", str(config))

    init_local_db()
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value_json, updated_at)
            VALUES (?, ?, ?)
            """,
            ("bindery_api_key", json.dumps("legacy-secret"), "test"),
        )
        conn.commit()

    init_local_db()

    with local_conn() as conn:
        row = conn.execute(
            "SELECT key FROM app_settings WHERE key=?",
            ("bindery_api_key",),
        ).fetchone()
    assert row is None


def test_save_settings_never_stores_bindery_api_key(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setattr(settings, "config_dir", str(config))
    init_local_db()

    save_persisted_settings(
        {
            "bindery_url": "http://bindery:8787",
            "bindery_api_key": "must-not-be-stored",
        }
    )

    persisted = load_persisted_settings()
    assert persisted["bindery_url"] == "http://bindery:8787"
    assert "bindery_api_key" not in persisted

    with local_conn() as conn:
        row = conn.execute(
            "SELECT key FROM app_settings WHERE key=?",
            ("bindery_api_key",),
        ).fetchone()
    assert row is None



def test_init_purges_legacy_persisted_tika_url(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setattr(settings, "config_dir", str(config))

    init_local_db()
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                "verification_tika_url",
                json.dumps("https://untrusted.example/upload"),
                "test",
            ),
        )
        conn.commit()

    init_local_db()

    with local_conn() as conn:
        row = conn.execute(
            "SELECT key FROM app_settings WHERE key=?",
            ("verification_tika_url",),
        ).fetchone()
    assert row is None


def test_save_settings_never_stores_tika_url(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setattr(settings, "config_dir", str(config))
    init_local_db()

    save_persisted_settings(
        {
            "bindery_url": "http://bindery:8787",
            "verification_tika_url": "https://untrusted.example/upload",
        }
    )

    persisted = load_persisted_settings()
    assert persisted["bindery_url"] == "http://bindery:8787"
    assert "verification_tika_url" not in persisted
