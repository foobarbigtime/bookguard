import sqlite3

import app.actions as actions
from app.config import Settings, settings


def _reset_settings():
    defaults = Settings()
    settings.__dict__.update(defaults.__dict__)


def setup_function():
    _reset_settings()


def _missing_result(tmp_path):
    return {
        "id": 101,
        "scan_id": "scan-1",
        "file_id": 202,
        "book_id": 303,
        "classification": "MISSING",
        "format": "ebook",
        "author": "Example Author",
        "title": "Example Book",
        "stored_path": "/data/media/books/Example Author/Example Book.epub",
        "local_path": str(tmp_path / "Example Book.epub"),
    }


def test_resolve_bindery_api_key_from_persisted_db(tmp_path):
    bindery_db = tmp_path / "bindery.db"
    conn = sqlite3.connect(bindery_db)
    conn.execute("CREATE TABLE settings_store(key TEXT, value TEXT)")
    conn.execute(
        "INSERT INTO settings_store(key, value) VALUES (?, ?)",
        ("auth.api_key", "a" * 64),
    )
    conn.commit()
    conn.close()

    settings.bindery_db = str(bindery_db)
    settings.bindery_api_key = ""

    assert actions.resolve_bindery_api_key() == "a" * 64


def test_missing_detach_preview_safe_only_for_exact_absent_association(tmp_path, monkeypatch):
    result = _missing_result(tmp_path)

    monkeypatch.setattr(
        actions,
        "latest_scan",
        lambda: {"id": "scan-1", "status": "complete"},
    )
    monkeypatch.setattr(
        actions,
        "bindery_file_by_id",
        lambda file_id: {
            "file_id": 202,
            "book_id": 303,
            "format": "ebook",
            "stored_path": result["stored_path"],
        },
    )

    preview = actions.missing_detach_preview(result)

    assert preview["eligible"] is True
    assert preview["safe"] is True
    assert preview["state"] == "safe"
    assert preview["physical_exists"] is False
    assert preview["exact_db_match"] is True


def test_missing_detach_preview_refuses_when_path_reappears(tmp_path, monkeypatch):
    result = _missing_result(tmp_path)
    path = tmp_path / "Example Book.epub"
    path.write_text("present", encoding="utf-8")

    monkeypatch.setattr(
        actions,
        "latest_scan",
        lambda: {"id": "scan-1", "status": "complete"},
    )
    monkeypatch.setattr(
        actions,
        "bindery_file_by_id",
        lambda file_id: {
            "file_id": 202,
            "book_id": 303,
            "format": "ebook",
            "stored_path": result["stored_path"],
        },
    )

    preview = actions.missing_detach_preview(result)

    assert preview["safe"] is False
    assert preview["state"] == "path_exists"


def test_missing_detach_preview_marks_already_detached_stale_scan(tmp_path, monkeypatch):
    result = _missing_result(tmp_path)

    monkeypatch.setattr(
        actions,
        "latest_scan",
        lambda: {"id": "scan-1", "status": "complete"},
    )
    monkeypatch.setattr(actions, "bindery_file_by_id", lambda file_id: None)

    preview = actions.missing_detach_preview(result)

    assert preview["safe"] is False
    assert preview["state"] == "already_detached"
