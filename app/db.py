from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import sqlite3

from .config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def bindery_conn():
    uri = f"file:{settings.bindery_db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def load_bindery_files() -> list[dict]:
    with bindery_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                bf.id AS file_id,
                bf.book_id,
                bf.format,
                bf.path AS stored_path,
                b.title,
                b.status,
                a.name AS author
            FROM book_files bf
            JOIN books b ON b.id = bf.book_id
            JOIN authors a ON a.id = b.author_id
            ORDER BY a.name COLLATE NOCASE, b.title COLLATE NOCASE, bf.id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def associations_inside_path(stored_path: str) -> list[dict]:
    escaped = stored_path.rstrip("/")
    like = escaped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
    with bindery_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                bf.id AS file_id,
                bf.book_id,
                bf.format,
                bf.path AS stored_path,
                b.title,
                a.name AS author
            FROM book_files bf
            JOIN books b ON b.id = bf.book_id
            JOIN authors a ON a.id = b.author_id
            WHERE bf.path = ?
               OR bf.path LIKE ? ESCAPE '\\'
            ORDER BY bf.id
            """,
            (escaped, like),
        ).fetchall()
    return [dict(r) for r in rows]


def local_db_path() -> str:
    os.makedirs(settings.config_dir, exist_ok=True)
    return os.path.join(settings.config_dir, "bookguard.db")


@contextmanager
def local_conn():
    conn = sqlite3.connect(local_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_local_db() -> None:
    with local_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                file_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                author TEXT NOT NULL,
                title TEXT NOT NULL,
                format TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                local_path TEXT NOT NULL,
                classification TEXT NOT NULL,
                risk_score INTEGER NOT NULL,
                reasons_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                scanned_at TEXT NOT NULL,
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            );

            CREATE INDEX IF NOT EXISTS idx_scan_results_scan
                ON scan_results(scan_id);
            CREATE INDEX IF NOT EXISTS idx_scan_results_class
                ON scan_results(scan_id, classification);
            """
        )
        conn.commit()


def create_scan(scan_id: str, total: int) -> None:
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO scans(id, started_at, status, total, processed)
            VALUES (?, ?, 'running', ?, 0)
            """,
            (scan_id, utc_now(), total),
        )
        conn.commit()


def update_scan_progress(scan_id: str, processed: int) -> None:
    with local_conn() as conn:
        conn.execute("UPDATE scans SET processed=? WHERE id=?", (processed, scan_id))
        conn.commit()


def finish_scan(scan_id: str, status: str = "complete", error: str | None = None) -> None:
    with local_conn() as conn:
        conn.execute(
            """
            UPDATE scans
            SET finished_at=?, status=?, error=?
            WHERE id=?
            """,
            (utc_now(), status, error, scan_id),
        )
        conn.commit()


def add_result(scan_id: str, result: dict) -> None:
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO scan_results(
                scan_id, file_id, book_id, author, title, format,
                stored_path, local_path, classification, risk_score,
                reasons_json, metadata_json, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                result["file_id"],
                result["book_id"],
                result["author"],
                result["title"],
                result["format"],
                result["stored_path"],
                result["local_path"],
                result["classification"],
                result["risk_score"],
                json.dumps(result.get("reasons", []), ensure_ascii=False),
                json.dumps(result.get("metadata", {}), ensure_ascii=False),
                utc_now(),
            ),
        )
        conn.commit()


def latest_scan() -> dict | None:
    with local_conn() as conn:
        row = conn.execute("SELECT * FROM scans ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def latest_results(classification: str | None = None, limit: int = 1000) -> list[dict]:
    scan = latest_scan()
    if not scan:
        return []
    params: list[object] = [scan["id"]]
    where = "scan_id=?"
    if classification:
        where += " AND classification=?"
        params.append(classification)
    params.append(limit)
    with local_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM scan_results
            WHERE {where}
            ORDER BY risk_score DESC, author COLLATE NOCASE, title COLLATE NOCASE
            LIMIT ?
            """,
            params,
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["reasons"] = json.loads(item.pop("reasons_json"))
        item["metadata"] = json.loads(item.pop("metadata_json"))
        out.append(item)
    return out


def latest_counts() -> dict[str, int]:
    scan = latest_scan()
    if not scan:
        return {}
    with local_conn() as conn:
        rows = conn.execute(
            """
            SELECT classification, COUNT(*) AS n
            FROM scan_results
            WHERE scan_id=?
            GROUP BY classification
            """,
            (scan["id"],),
        ).fetchall()
    return {r["classification"]: r["n"] for r in rows}


def result_by_id(result_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute("SELECT * FROM scan_results WHERE id=?", (result_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["reasons"] = json.loads(item.pop("reasons_json"))
    item["metadata"] = json.loads(item.pop("metadata_json"))
    return item
