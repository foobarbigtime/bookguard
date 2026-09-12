import sqlite3

import app.verifier as verifier


def test_verification_summary_counts_only_latest_result_per_item(tmp_path, monkeypatch):
    db_path = tmp_path / "bookguard.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE content_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL,
            result_id INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO content_verifications(scan_id, result_id, verdict, updated_at) VALUES (?, ?, ?, ?)",
        [
            ("scan-current", 1, "METADATA_ERROR", "2026-09-10T10:00:00+00:00"),
            ("scan-current", 1, "INSUFFICIENT_EVIDENCE", "2026-09-10T11:00:00+00:00"),
            ("scan-current", 2, "WRONG_CONTENT", "2026-09-10T10:30:00+00:00"),
            ("scan-current", 3, "METADATA_ERROR", "2026-09-10T10:45:00+00:00"),
            ("scan-old", 4, "WRONG_CONTENT", "2026-09-10T12:00:00+00:00"),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(verifier, "init_verification_db", lambda: None)
    monkeypatch.setattr(verifier, "latest_scan", lambda: {"id": "scan-current"})

    def local_conn():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(verifier, "local_conn", local_conn)

    assert verifier.verification_summary() == {
        "INSUFFICIENT_EVIDENCE": 1,
        "METADATA_ERROR": 1,
        "WRONG_CONTENT": 1,
    }
