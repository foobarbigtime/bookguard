from __future__ import annotations

import json
from typing import Any

from .db import local_conn, utc_now


CACHE_SCHEMA_VERSION = 1


def _ensure_table() -> None:
    with local_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audiobook_probe_cache (
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
            "CREATE INDEX IF NOT EXISTS idx_audiobook_probe_cache_updated "
            "ON audiobook_probe_cache(updated_at DESC)"
        )
        conn.commit()


def cached_probe(path: str, size_bytes: int, modified_ns: int) -> dict[str, Any] | None:
    _ensure_table()
    with local_conn() as conn:
        row = conn.execute(
            """
            SELECT probe_json
            FROM audiobook_probe_cache
            WHERE path=? AND size_bytes=? AND modified_ns=? AND schema_version=?
            """,
            (path, size_bytes, modified_ns, CACHE_SCHEMA_VERSION),
        ).fetchone()
    if not row:
        return None
    try:
        probe = json.loads(row["probe_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return probe if isinstance(probe, dict) else None


def save_probe(path: str, size_bytes: int, modified_ns: int, probe: dict[str, Any]) -> None:
    _ensure_table()
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO audiobook_probe_cache(
                path, size_bytes, modified_ns, schema_version, probe_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size_bytes=excluded.size_bytes,
                modified_ns=excluded.modified_ns,
                schema_version=excluded.schema_version,
                probe_json=excluded.probe_json,
                updated_at=excluded.updated_at
            """,
            (
                path,
                size_bytes,
                modified_ns,
                CACHE_SCHEMA_VERSION,
                json.dumps(probe, ensure_ascii=False),
                utc_now(),
            ),
        )
        conn.commit()


def cache_count() -> int:
    _ensure_table()
    with local_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM audiobook_probe_cache").fetchone()
    return int(row["n"] if row else 0)
