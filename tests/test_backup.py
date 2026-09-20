from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from tools.bookguard_backup import (
    BackupError,
    DATABASE_NAME,
    MANIFEST_NAME,
    REQUIRED_TABLES,
    create_backup,
    validate_backup,
    validate_restore,
)


def _make_source(config: Path) -> Path:
    config.mkdir(parents=True)
    database = config / DATABASE_NAME
    with sqlite3.connect(database) as conn:
        for table in sorted(REQUIRED_TABLES):
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT)')
        conn.execute("INSERT INTO scans(value) VALUES ('before backup')")
        conn.commit()
    return database



def _make_real_source(config: Path) -> Path:
    from app.config import settings
    from app.db import init_local_db

    config.mkdir(parents=True)
    original = settings.config_dir
    try:
        settings.config_dir = str(config)
        init_local_db()
    finally:
        settings.config_dir = original
    return config / DATABASE_NAME


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def test_create_and_validate_consistent_backup(tmp_path):
    config = tmp_path / "config"
    backups = tmp_path / "backups"
    source = _make_source(config)

    created = create_backup(config, backups, name="known-good")
    bundle = Path(created["bundle"])

    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE scans SET value='after backup' WHERE id=1")
        conn.commit()

    validated = validate_backup(bundle)

    assert validated["ok"] is True
    assert validated["readOnlyValidation"] is True
    assert validated["database"]["integrity"] == "ok"
    assert set(REQUIRED_TABLES) <= set(validated["database"]["tables"])
    assert (bundle / MANIFEST_NAME).is_file()
    assert (bundle / DATABASE_NAME).is_file()

    with sqlite3.connect(
        (bundle / DATABASE_NAME).resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
    ) as conn:
        value = conn.execute("SELECT value FROM scans WHERE id=1").fetchone()[0]
    assert value == "before backup"


def test_validate_rejects_tampered_database(tmp_path):
    config = tmp_path / "config"
    backups = tmp_path / "backups"
    _make_source(config)
    bundle = Path(create_backup(config, backups, name="tamper-test")["bundle"])

    database = bundle / DATABASE_NAME
    database.write_bytes(database.read_bytes() + b"tamper")

    with pytest.raises(BackupError, match="size mismatch"):
        validate_backup(bundle)


def test_validate_rejects_manifest_hash_change(tmp_path):
    config = tmp_path / "config"
    backups = tmp_path / "backups"
    _make_source(config)
    bundle = Path(create_backup(config, backups, name="hash-test")["bundle"])

    manifest_path = bundle / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="SHA-256"):
        validate_backup(bundle)


def test_create_rejects_non_bookguard_database(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    with sqlite3.connect(config / DATABASE_NAME) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()

    with pytest.raises(BackupError, match="missing required BookGuard tables"):
        create_backup(config, tmp_path / "backups", name="invalid")


def test_create_rejects_symlink_source(tmp_path):
    real = tmp_path / "real.db"
    with sqlite3.connect(real) as conn:
        conn.execute("CREATE TABLE anything (id INTEGER PRIMARY KEY)")
        conn.commit()

    config = tmp_path / "config"
    config.mkdir()
    (config / DATABASE_NAME).symlink_to(real)

    with pytest.raises(BackupError, match="must not be a symlink"):
        create_backup(config, tmp_path / "backups", name="symlink")


def test_backup_root_must_be_separate_from_config(tmp_path):
    config = tmp_path / "config"
    _make_source(config)

    with pytest.raises(BackupError, match="must be separate"):
        create_backup(config, config, name="bad-location")


def test_backup_helper_shell_syntax():
    completed = subprocess.run(
        ["bash", "-n", "scripts/bookguard-backup.sh"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_restore_validation_uses_disposable_copy_and_preserves_backup(tmp_path):
    config = tmp_path / "config"
    backups = tmp_path / "backups"
    _make_real_source(config)
    bundle = Path(create_backup(config, backups, name="restore-test")["bundle"])
    backup_db = bundle / DATABASE_NAME
    before = _file_sha256(backup_db)

    restore_dir = tmp_path / "restore"
    result = validate_restore(bundle, restore_dir)

    assert result["ok"] is True
    assert result["backupUnchanged"] is True
    assert result["startupInitialization"] is True
    assert result["productionStateTouched"] is False
    assert result["disposableRestore"] is True
    assert result["restoredDatabase"]["integrity"] == "ok"
    assert _file_sha256(backup_db) == before
    assert (restore_dir / DATABASE_NAME).is_file()


def test_restore_validation_rejects_nonempty_destination(tmp_path):
    config = tmp_path / "config"
    backups = tmp_path / "backups"
    _make_real_source(config)
    bundle = Path(create_backup(config, backups, name="restore-nonempty")["bundle"])

    restore_dir = tmp_path / "restore"
    restore_dir.mkdir()
    (restore_dir / "keep.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(BackupError, match="must be empty"):
        validate_restore(bundle, restore_dir)

    assert (restore_dir / "keep.txt").read_text(encoding="utf-8") == "do not overwrite"
