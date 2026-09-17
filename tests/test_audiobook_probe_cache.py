from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3

import app.audiobook_probe_cache as cache_module


def _use_temp_cache_db(tmp_path, monkeypatch) -> Path:
    db_path = tmp_path / "bookguard.db"

    @contextmanager
    def temp_local_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(cache_module, "local_conn", temp_local_conn)
    return db_path


def _probe(path: Path) -> dict:
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "duration_seconds": 60.0,
        "audio_stream_count": 1,
        "codec": "mp3",
    }


def test_cache_reuses_probe_for_exact_same_file_identity(tmp_path, monkeypatch):
    _use_temp_cache_db(tmp_path, monkeypatch)
    path = tmp_path / "track.mp3"
    path.write_bytes(b"audio-data")
    stat = path.stat()
    probe = _probe(path)

    assert cache_module.save_probe(str(path), stat.st_size, stat.st_mtime_ns, probe) is True
    assert cache_module.cached_probe(str(path), stat.st_size, stat.st_mtime_ns) == probe
    assert cache_module.cache_count() == 1


def test_cache_rejects_replaced_file_with_same_size_and_mtime(tmp_path, monkeypatch):
    _use_temp_cache_db(tmp_path, monkeypatch)
    path = tmp_path / "track.mp3"
    path.write_bytes(b"AAAA")
    original = path.stat()
    probe = _probe(path)
    assert cache_module.save_probe(
        str(path), original.st_size, original.st_mtime_ns, probe
    ) is True

    replacement = tmp_path / "replacement.mp3"
    replacement.write_bytes(b"BBBB")
    os.utime(
        replacement,
        ns=(replacement.stat().st_atime_ns, original.st_mtime_ns),
    )
    os.replace(replacement, path)
    current = path.stat()

    assert current.st_size == original.st_size
    assert current.st_mtime_ns == original.st_mtime_ns
    assert (current.st_ino, current.st_ctime_ns) != (original.st_ino, original.st_ctime_ns)
    assert cache_module.cached_probe(str(path), current.st_size, current.st_mtime_ns) is None


def test_cache_migrates_version_one_table_but_ignores_old_rows(tmp_path, monkeypatch):
    db_path = _use_temp_cache_db(tmp_path, monkeypatch)
    path = tmp_path / "track.mp3"
    path.write_bytes(b"audio")
    stat = path.stat()

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE audiobook_probe_cache (
            path TEXT PRIMARY KEY,
            size_bytes INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            probe_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO audiobook_probe_cache(
            path, size_bytes, modified_ns, schema_version, probe_json, updated_at
        ) VALUES (?, ?, ?, 1, ?, ?)
        """,
        (
            str(path),
            stat.st_size,
            stat.st_mtime_ns,
            json.dumps(_probe(path)),
            "2026-09-17T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    assert cache_module.cached_probe(str(path), stat.st_size, stat.st_mtime_ns) is None

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(audiobook_probe_cache)")}
    conn.close()
    assert {"ctime_ns", "device_id", "inode"}.issubset(columns)
    assert cache_module.cache_count() == 0


def test_cache_refuses_save_when_basic_stat_changed(tmp_path, monkeypatch):
    _use_temp_cache_db(tmp_path, monkeypatch)
    path = tmp_path / "track.mp3"
    path.write_bytes(b"audio")
    stat = path.stat()
    path.write_bytes(b"audio-changed")

    assert cache_module.save_probe(
        str(path), stat.st_size, stat.st_mtime_ns, {"path": str(path)}
    ) is False
    assert cache_module.cache_count() == 0
