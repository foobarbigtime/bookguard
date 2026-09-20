from __future__ import annotations

import json
import os
from typing import Any

from .db import local_conn, utc_now


CACHE_SCHEMA_VERSION = 2
_IDENTITY_COLUMNS = {
    "ctime_ns": "INTEGER NOT NULL DEFAULT 0",
    "device_id": "INTEGER NOT NULL DEFAULT 0",
    "inode": "INTEGER NOT NULL DEFAULT 0",
}


def _ensure_table() -> None:
    with local_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audiobook_probe_cache (
                path TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL DEFAULT 0,
                device_id INTEGER NOT NULL DEFAULT 0,
                inode INTEGER NOT NULL DEFAULT 0,
                schema_version INTEGER NOT NULL,
                probe_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        # Version 1 existed before the stronger filesystem identity fields were
        # introduced. Add the columns in place so upgrades never need to drop
        # the persistent cache table. Old rows remain schema_version=1 and are
        # therefore ignored until a fresh probe replaces them.
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(audiobook_probe_cache)").fetchall()
        }
        for name, definition in _IDENTITY_COLUMNS.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE audiobook_probe_cache ADD COLUMN {name} {definition}"
                )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audiobook_probe_cache_updated "
            "ON audiobook_probe_cache(updated_at DESC)"
        )
        conn.commit()


def _file_identity(
    path: str,
    expected_size_bytes: int,
    expected_modified_ns: int,
) -> tuple[int, int, int] | None:
    """Return ctime/device/inode only when the caller's basic stat is still current."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    if stat.st_size != expected_size_bytes or stat.st_mtime_ns != expected_modified_ns:
        return None
    return stat.st_ctime_ns, stat.st_dev, stat.st_ino


def cached_probe(path: str, size_bytes: int, modified_ns: int) -> dict[str, Any] | None:
    _ensure_table()
    identity = _file_identity(path, size_bytes, modified_ns)
    if identity is None:
        return None
    ctime_ns, device_id, inode = identity

    with local_conn() as conn:
        row = conn.execute(
            """
            SELECT probe_json
            FROM audiobook_probe_cache
            WHERE path=?
              AND size_bytes=?
              AND modified_ns=?
              AND ctime_ns=?
              AND device_id=?
              AND inode=?
              AND schema_version=?
            """,
            (
                path,
                size_bytes,
                modified_ns,
                ctime_ns,
                device_id,
                inode,
                CACHE_SCHEMA_VERSION,
            ),
        ).fetchone()
    if not row:
        return None
    try:
        probe = json.loads(row["probe_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return probe if isinstance(probe, dict) else None


def save_probe(
    path: str,
    size_bytes: int,
    modified_ns: int,
    probe: dict[str, Any],
) -> bool:
    """Save a probe only if the file still matches the stat used to select it."""
    _ensure_table()
    identity = _file_identity(path, size_bytes, modified_ns)
    if identity is None:
        return False
    ctime_ns, device_id, inode = identity

    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO audiobook_probe_cache(
                path, size_bytes, modified_ns, ctime_ns, device_id, inode,
                schema_version, probe_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size_bytes=excluded.size_bytes,
                modified_ns=excluded.modified_ns,
                ctime_ns=excluded.ctime_ns,
                device_id=excluded.device_id,
                inode=excluded.inode,
                schema_version=excluded.schema_version,
                probe_json=excluded.probe_json,
                updated_at=excluded.updated_at
            """,
            (
                path,
                size_bytes,
                modified_ns,
                ctime_ns,
                device_id,
                inode,
                CACHE_SCHEMA_VERSION,
                json.dumps(probe, ensure_ascii=False),
                utc_now(),
            ),
        )
        conn.commit()
    return True


def cache_count() -> int:
    _ensure_table()
    with local_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM audiobook_probe_cache WHERE schema_version=?",
            (CACHE_SCHEMA_VERSION,),
        ).fetchone()
    return int(row["n"] if row else 0)
