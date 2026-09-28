from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import sqlite3

from .config import settings


NON_PERSISTED_SETTING_KEYS = {"bindery_api_key", "verification_tika_url"}


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


def bindery_file_by_id(file_id: int) -> dict | None:
    """Read one current Bindery file association by its stable row id."""
    with bindery_conn() as conn:
        row = conn.execute(
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
            WHERE bf.id = ?
            LIMIT 1
            """,
            (file_id,),
        ).fetchone()
    return dict(row) if row else None


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
                reason_code TEXT NOT NULL DEFAULT 'UNKNOWN',
                reasons_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                scanned_at TEXT NOT NULL,
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metadata_repairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id INTEGER NOT NULL,
                scan_id TEXT NOT NULL,
                file_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                format TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                local_path TEXT NOT NULL,
                repair_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                undone_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS cleanup_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id INTEGER NOT NULL,
                scan_id TEXT NOT NULL,
                file_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                classification TEXT NOT NULL,
                format TEXT NOT NULL,
                author TEXT NOT NULL,
                title TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                local_path TEXT NOT NULL,
                action_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS ebook_admissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id INTEGER NOT NULL,
                scan_id TEXT NOT NULL,
                book_id INTEGER NOT NULL,
                staged_relative_path TEXT NOT NULL,
                staged_sha256 TEXT,
                stored_path TEXT NOT NULL,
                local_path TEXT NOT NULL,
                status TEXT NOT NULL,
                publication_method TEXT,
                failure_stage TEXT,
                verification_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS automation_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL UNIQUE,
                policy_version TEXT NOT NULL,
                mode TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                result_id INTEGER,
                book_id INTEGER,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                observed_count INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS recovery_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL UNIQUE,
                planner_version TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                result_id INTEGER,
                book_id INTEGER,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                plan_kind TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                state TEXT NOT NULL,
                evidence_revision TEXT NOT NULL,
                preconditions_json TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                current_step INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                final_outcome TEXT
            );

            CREATE TABLE IF NOT EXISTS recovery_plan_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                from_step INTEGER NOT NULL,
                to_step INTEGER NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES recovery_plans(id)
            );

            CREATE TABLE IF NOT EXISTS automatic_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                plan_signature TEXT NOT NULL,
                action_code TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                state TEXT NOT NULL,
                evidence_revision TEXT NOT NULL,
                boundary_json TEXT NOT NULL DEFAULT '{}',
                external_result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(plan_id) REFERENCES recovery_plans(id),
                UNIQUE(plan_signature, action_code, step_index)
            );

            CREATE INDEX IF NOT EXISTS idx_automatic_executions_plan
                ON automatic_executions(plan_id, step_index, id);

            CREATE TABLE IF NOT EXISTS ebook_acquisitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id INTEGER NOT NULL,
                scan_id TEXT NOT NULL,
                book_id INTEGER NOT NULL,
                candidate_guid TEXT NOT NULL,
                candidate_title TEXT NOT NULL,
                candidate_indexer TEXT,
                candidate_protocol TEXT,
                replacement_for_acquisition_id INTEGER,
                replacement_for_quarantine_plan_id INTEGER,
                status TEXT NOT NULL,
                queue_id INTEGER,
                queue_status TEXT,
                grab_response_json TEXT,
                observed_relative_path TEXT,
                observed_size INTEGER,
                observed_modified_ns INTEGER,
                staged_relative_path TEXT,
                staged_sha256 TEXT,
                verification_json TEXT,
                admission_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS alternate_candidate_selections (
                acquisition_id INTEGER PRIMARY KEY,
                plan_id INTEGER NOT NULL,
                plan_signature TEXT NOT NULL,
                evidence_revision TEXT NOT NULL,
                candidate_guid TEXT NOT NULL,
                candidate_title TEXT NOT NULL,
                candidate_protocol TEXT NOT NULL,
                candidate_indexer TEXT NOT NULL,
                candidate_fingerprint TEXT NOT NULL,
                selected_at TEXT NOT NULL,
                FOREIGN KEY(acquisition_id) REFERENCES ebook_acquisitions(id),
                FOREIGN KEY(plan_id) REFERENCES recovery_plans(id)
            );

            CREATE TABLE IF NOT EXISTS quarantine_replacement_selections (
                result_id INTEGER PRIMARY KEY,
                plan_id INTEGER NOT NULL,
                plan_signature TEXT NOT NULL,
                evidence_revision TEXT NOT NULL,
                quarantine_execution_id INTEGER NOT NULL,
                quarantine_sha256 TEXT NOT NULL,
                candidate_guid TEXT NOT NULL,
                candidate_title TEXT NOT NULL,
                candidate_protocol TEXT NOT NULL,
                candidate_indexer TEXT NOT NULL,
                candidate_fingerprint TEXT NOT NULL,
                selected_at TEXT NOT NULL,
                FOREIGN KEY(result_id) REFERENCES scan_results(id),
                FOREIGN KEY(plan_id) REFERENCES recovery_plans(id),
                FOREIGN KEY(quarantine_execution_id) REFERENCES automatic_executions(id)
            );
            """
        )

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(scan_results)").fetchall()
        }
        if "reason_code" not in columns:
            conn.execute(
                "ALTER TABLE scan_results ADD COLUMN reason_code TEXT NOT NULL DEFAULT 'UNKNOWN'"
            )

        admission_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(ebook_admissions)").fetchall()
        }
        if "publication_method" not in admission_columns:
            conn.execute(
                "ALTER TABLE ebook_admissions ADD COLUMN publication_method TEXT"
            )
        if "failure_stage" not in admission_columns:
            conn.execute(
                "ALTER TABLE ebook_admissions ADD COLUMN failure_stage TEXT"
            )

        acquisition_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(ebook_acquisitions)").fetchall()
        }
        if "replacement_for_acquisition_id" not in acquisition_columns:
            conn.execute(
                "ALTER TABLE ebook_acquisitions "
                "ADD COLUMN replacement_for_acquisition_id INTEGER"
            )
        if "replacement_for_quarantine_plan_id" not in acquisition_columns:
            conn.execute(
                "ALTER TABLE ebook_acquisitions "
                "ADD COLUMN replacement_for_quarantine_plan_id INTEGER"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ebook_replacement_parent "
            "ON ebook_acquisitions(replacement_for_acquisition_id) "
            "WHERE replacement_for_acquisition_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_quarantine_replacement_plan "
            "ON ebook_acquisitions(replacement_for_quarantine_plan_id) "
            "WHERE replacement_for_quarantine_plan_id IS NOT NULL"
        )

        recovery_plan_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(recovery_plans)").fetchall()
        }
        if "final_outcome" not in recovery_plan_columns:
            conn.execute(
                "ALTER TABLE recovery_plans ADD COLUMN final_outcome TEXT"
            )

        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_scan_results_scan
                ON scan_results(scan_id);
            CREATE INDEX IF NOT EXISTS idx_scan_results_class
                ON scan_results(scan_id, classification);
            CREATE INDEX IF NOT EXISTS idx_scan_results_reason
                ON scan_results(scan_id, reason_code);
            CREATE INDEX IF NOT EXISTS idx_metadata_repairs_created
                ON metadata_repairs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_metadata_repairs_result
                ON metadata_repairs(result_id);
            CREATE INDEX IF NOT EXISTS idx_cleanup_actions_created
                ON cleanup_actions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_cleanup_actions_result
                ON cleanup_actions(result_id);
            CREATE INDEX IF NOT EXISTS idx_ebook_admissions_created
                ON ebook_admissions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ebook_admissions_result
                ON ebook_admissions(result_id);
            CREATE INDEX IF NOT EXISTS idx_automation_observations_seen
                ON automation_observations(last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_automation_observations_subject
                ON automation_observations(subject_kind, subject_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_automation_observations_decision
                ON automation_observations(decision, last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recovery_plans_updated
                ON recovery_plans(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recovery_plans_subject
                ON recovery_plans(subject_kind, subject_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_recovery_plans_state
                ON recovery_plans(state, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_recovery_plans_result
                ON recovery_plans(result_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_recovery_plan_transitions_plan
                ON recovery_plan_transitions(plan_id, id);
            CREATE INDEX IF NOT EXISTS idx_recovery_plan_transitions_created
                ON recovery_plan_transitions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ebook_acquisitions_created
                ON ebook_acquisitions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ebook_acquisitions_result
                ON ebook_acquisitions(result_id);
            CREATE INDEX IF NOT EXISTS idx_ebook_acquisitions_status
                ON ebook_acquisitions(status);
            """
        )
        for key in NON_PERSISTED_SETTING_KEYS:
            conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
        conn.commit()


def load_persisted_settings() -> dict:
    with local_conn() as conn:
        rows = conn.execute("SELECT key, value_json FROM app_settings").fetchall()
    out = {}
    for row in rows:
        key = str(row["key"])
        if key in NON_PERSISTED_SETTING_KEYS:
            continue
        try:
            out[key] = json.loads(row["value_json"])
        except json.JSONDecodeError:
            continue
    return out


def save_persisted_settings(values: dict) -> None:
    now = utc_now()
    with local_conn() as conn:
        for key in NON_PERSISTED_SETTING_KEYS:
            conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
        for key, value in values.items():
            if key in NON_PERSISTED_SETTING_KEYS:
                continue
            conn.execute(
                """
                INSERT INTO app_settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )
        conn.commit()


def clear_persisted_settings() -> None:
    with local_conn() as conn:
        conn.execute("DELETE FROM app_settings")
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
                stored_path, local_path, classification, risk_score, reason_code,
                reasons_json, metadata_json, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                result.get("reason_code", "UNKNOWN"),
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


def _decode_results(rows) -> list[dict]:
    out = []
    for row in rows:
        item = dict(row)
        item["reasons"] = json.loads(item.pop("reasons_json"))
        item["metadata"] = json.loads(item.pop("metadata_json"))
        item["reason_code"] = item.get("reason_code") or "UNKNOWN"
        out.append(item)
    return out


def latest_results(
    classification: str | None = None,
    reason_code: str | None = None,
    limit: int = 1000,
) -> list[dict]:
    scan = latest_scan()
    if not scan:
        return []
    params: list[object] = [scan["id"]]
    where = "scan_id=?"
    if classification:
        where += " AND classification=?"
        params.append(classification)
    if reason_code:
        where += " AND reason_code=?"
        params.append(reason_code)
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
    return _decode_results(rows)


def latest_recent_results(limit: int = 20) -> list[dict]:
    scan = latest_scan()
    if not scan:
        return []
    with local_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM scan_results
            WHERE scan_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (scan["id"], limit),
        ).fetchall()
    return _decode_results(rows)


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


def latest_reason_counts(classification: str | None = None) -> dict[str, int]:
    scan = latest_scan()
    if not scan:
        return {}
    params: list[object] = [scan["id"]]
    where = "scan_id=?"
    if classification:
        where += " AND classification=?"
        params.append(classification)
    with local_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT reason_code, COUNT(*) AS n
            FROM scan_results
            WHERE {where}
            GROUP BY reason_code
            ORDER BY n DESC, reason_code
            """,
            params,
        ).fetchall()
    return {(r["reason_code"] or "UNKNOWN"): r["n"] for r in rows}


def result_by_id(result_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute("SELECT * FROM scan_results WHERE id=?", (result_id,)).fetchone()
    if not row:
        return None
    return _decode_results([row])[0]


def create_ebook_admission(result: dict, staged_relative_path: str) -> int:
    now = utc_now()
    with local_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO ebook_admissions(
                result_id, scan_id, book_id, staged_relative_path,
                stored_path, local_path, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'preparing', ?, ?)
            """,
            (
                result["id"],
                result["scan_id"],
                result["book_id"],
                staged_relative_path,
                result["stored_path"],
                result["local_path"],
                now,
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def update_ebook_admission(
    admission_id: int,
    status: str,
    *,
    staged_sha256: str | None = None,
    publication_method: str | None = None,
    failure_stage: str | None = None,
    verification: dict | None = None,
    error: str | None = None,
) -> None:
    with local_conn() as conn:
        conn.execute(
            """
            UPDATE ebook_admissions
            SET status=?, staged_sha256=COALESCE(?, staged_sha256),
                publication_method=COALESCE(?, publication_method),
                failure_stage=COALESCE(?, failure_stage),
                verification_json=COALESCE(?, verification_json),
                updated_at=?, error=?
            WHERE id=?
            """,
            (
                status,
                staged_sha256,
                publication_method,
                failure_stage,
                json.dumps(verification, ensure_ascii=False) if verification is not None else None,
                utc_now(),
                error,
                admission_id,
            ),
        )
        conn.commit()


def ebook_admission_by_id(admission_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ebook_admissions WHERE id=?",
            (admission_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    raw = item.pop("verification_json")
    item["verification"] = json.loads(raw) if raw else None
    return item


def recent_ebook_admissions(limit: int = 100) -> list[dict]:
    with local_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ebook_admissions ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        raw = item.pop("verification_json")
        item["verification"] = json.loads(raw) if raw else None
        items.append(item)
    return items


def _decode_ebook_acquisition(row) -> dict:
    item = dict(row)
    for source, target in (
        ("grab_response_json", "grab_response"),
        ("verification_json", "verification"),
    ):
        raw = item.pop(source)
        item[target] = json.loads(raw) if raw else None
    return item


def create_ebook_acquisition(
    result: dict,
    candidate: dict,
    *,
    replacement_for_acquisition_id: int | None = None,
    replacement_for_quarantine_plan_id: int | None = None,
) -> int:
    now = utc_now()
    with local_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO ebook_acquisitions(
                result_id, scan_id, book_id, candidate_guid, candidate_title,
                candidate_indexer, candidate_protocol,
                replacement_for_acquisition_id,
                replacement_for_quarantine_plan_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?)
            """,
            (
                result["id"],
                result["scan_id"],
                result["book_id"],
                str(candidate.get("guid") or ""),
                str(candidate.get("title") or ""),
                str(candidate.get("indexerName") or candidate.get("indexer") or ""),
                str(candidate.get("protocol") or ""),
                replacement_for_acquisition_id,
                replacement_for_quarantine_plan_id,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def ebook_replacement_for_acquisition(acquisition_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ebook_acquisitions "
            "WHERE replacement_for_acquisition_id=? LIMIT 1",
            (int(acquisition_id),),
        ).fetchone()
    return _decode_ebook_acquisition(row) if row else None


def ebook_replacement_for_quarantine_plan(plan_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ebook_acquisitions "
            "WHERE replacement_for_quarantine_plan_id=? LIMIT 1",
            (int(plan_id),),
        ).fetchone()
    return _decode_ebook_acquisition(row) if row else None


def update_ebook_acquisition(
    acquisition_id: int,
    status: str,
    *,
    queue_id: int | None = None,
    queue_status: str | None = None,
    grab_response: dict | list | None = None,
    observed_relative_path: str | None = None,
    observed_size: int | None = None,
    observed_modified_ns: int | None = None,
    staged_relative_path: str | None = None,
    staged_sha256: str | None = None,
    verification: dict | None = None,
    admission_id: int | None = None,
    error: str | None = None,
) -> None:
    with local_conn() as conn:
        conn.execute(
            """
            UPDATE ebook_acquisitions
            SET status=?,
                queue_id=COALESCE(?, queue_id),
                queue_status=COALESCE(?, queue_status),
                grab_response_json=COALESCE(?, grab_response_json),
                observed_relative_path=COALESCE(?, observed_relative_path),
                observed_size=COALESCE(?, observed_size),
                observed_modified_ns=COALESCE(?, observed_modified_ns),
                staged_relative_path=COALESCE(?, staged_relative_path),
                staged_sha256=COALESCE(?, staged_sha256),
                verification_json=COALESCE(?, verification_json),
                admission_id=COALESCE(?, admission_id),
                updated_at=?,
                error=?
            WHERE id=?
            """,
            (
                status,
                queue_id,
                queue_status,
                json.dumps(grab_response, ensure_ascii=False)
                if grab_response is not None
                else None,
                observed_relative_path,
                observed_size,
                observed_modified_ns,
                staged_relative_path,
                staged_sha256,
                json.dumps(verification, ensure_ascii=False)
                if verification is not None
                else None,
                admission_id,
                utc_now(),
                error,
                acquisition_id,
            ),
        )
        conn.commit()


def note_ebook_acquisition_admission_blocked(acquisition_id: int, error: str) -> None:
    """Record a refusal only while another worker has not admitted this item."""
    with local_conn() as conn:
        conn.execute(
            """UPDATE ebook_acquisitions SET error=?, updated_at=?
               WHERE id=? AND status='verified' AND admission_id IS NULL""",
            (str(error), utc_now(), int(acquisition_id)),
        )
        conn.commit()


def reset_ebook_acquisition_for_retry(acquisition_id: int) -> None:
    """Clear attempt-specific queue state before retrying the same durable acquisition."""
    with local_conn() as conn:
        conn.execute(
            """
            UPDATE ebook_acquisitions
            SET status='grab_requested',
                queue_id=NULL,
                queue_status=NULL,
                grab_response_json=NULL,
                updated_at=?,
                error=NULL
            WHERE id=?
            """,
            (utc_now(), int(acquisition_id)),
        )
        conn.commit()


def ebook_acquisition_by_id(acquisition_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ebook_acquisitions WHERE id=?",
            (acquisition_id,),
        ).fetchone()
    return _decode_ebook_acquisition(row) if row else None


def ebook_acquisition_by_admission_id(admission_id: int) -> dict | None:
    with local_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM ebook_acquisitions
            WHERE admission_id=?
            ORDER BY id DESC
            LIMIT 2
            """,
            (admission_id,),
        ).fetchall()
    if len(rows) > 1:
        raise sqlite3.IntegrityError(
            "Multiple ebook acquisitions reference the same admission."
        )
    return _decode_ebook_acquisition(rows[0]) if rows else None


def recent_ebook_acquisitions(limit: int = 100) -> list[dict]:
    with local_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ebook_acquisitions ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [_decode_ebook_acquisition(row) for row in rows]


def active_ebook_acquisitions() -> list[dict]:
    active_statuses = (
        "preparing",
        "grab_requested",
        "queued",
        "downloading",
        "awaiting_staging",
        "staging_observed",
        "verified",
        "admitted",
        "finalizing",
        "cleanup_required",
    )
    placeholders = ",".join("?" for _ in active_statuses)
    with local_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM ebook_acquisitions
            WHERE status IN ({placeholders})
            ORDER BY id
            """,
            active_statuses,
        ).fetchall()
    return [_decode_ebook_acquisition(row) for row in rows]


def create_cleanup_action(result: dict, action_kind: str) -> int:
    with local_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO cleanup_actions(
                result_id, scan_id, file_id, book_id, classification, format,
                author, title, stored_path, local_path, action_kind, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                result["id"], result["scan_id"], result["file_id"], result["book_id"],
                result["classification"], result["format"], result["author"], result["title"],
                result["stored_path"], result["local_path"], action_kind, utc_now(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def finish_cleanup_action(cleanup_id: int, status: str, error: str | None = None) -> None:
    with local_conn() as conn:
        conn.execute(
            """
            UPDATE cleanup_actions
            SET status=?, completed_at=?, error=?
            WHERE id=?
            """,
            (status, utc_now(), error, cleanup_id),
        )
        conn.commit()


def recent_cleanup_actions(limit: int = 100) -> list[dict]:
    with local_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cleanup_actions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_metadata_repair(result: dict, repair_kind: str, before: dict, after: dict) -> int:
    with local_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO metadata_repairs(
                result_id, scan_id, file_id, book_id, format, stored_path, local_path,
                repair_kind, status, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                result["id"], result["scan_id"], result["file_id"], result["book_id"],
                result["format"], result["stored_path"], result["local_path"], repair_kind,
                json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), utc_now(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def finish_metadata_repair(repair_id: int, status: str, error: str | None = None) -> None:
    with local_conn() as conn:
        conn.execute(
            """
            UPDATE metadata_repairs
            SET status=?, completed_at=?, error=?
            WHERE id=?
            """,
            (status, utc_now(), error, repair_id),
        )
        conn.commit()


def mark_metadata_repair_undone(repair_id: int) -> None:
    with local_conn() as conn:
        conn.execute(
            "UPDATE metadata_repairs SET status='undone', undone_at=? WHERE id=?",
            (utc_now(), repair_id),
        )
        conn.commit()


def _decode_repair(row) -> dict:
    item = dict(row)
    item["before"] = json.loads(item.pop("before_json"))
    item["after"] = json.loads(item.pop("after_json"))
    return item


def metadata_repair_by_id(repair_id: int) -> dict | None:
    with local_conn() as conn:
        row = conn.execute("SELECT * FROM metadata_repairs WHERE id=?", (repair_id,)).fetchone()
    return _decode_repair(row) if row else None


def recent_metadata_repairs(limit: int = 100) -> list[dict]:
    with local_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM metadata_repairs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_decode_repair(row) for row in rows]
