from __future__ import annotations

import json


from app.config import settings
from app.db import add_result, create_scan, finish_scan, init_local_db, local_conn
from app.observe import observe_attention_items, observe_snapshot, run_observe_cycle
from app.verifier import init_verification_db


def _seed_review_result(tmp_path, *, verdict="INSUFFICIENT_EVIDENCE"):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_verification_db()

    create_scan("observe-scan", 1)
    add_result(
        "observe-scan",
        {
            "file_id": 41,
            "book_id": 91,
            "author": "Example Author",
            "title": "Example Book",
            "format": "ebook",
            "stored_path": "/data/media/books/Example Book.epub",
            "local_path": "/books/Example Book.epub",
            "classification": "REVIEW",
            "risk_score": 70,
            "reason_code": "IDENTITY_REVIEW",
            "reasons": ["fixture"],
            "metadata": {},
        },
    )
    finish_scan("observe-scan")

    with local_conn() as conn:
        result_id = int(
            conn.execute(
                "SELECT id FROM scan_results WHERE scan_id='observe-scan'"
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO content_verifications(
                signature, result_id, scan_id, file_id, book_id, format, author,
                title, target_path, file_fingerprint, verdict, confidence,
                source, evidence_json, created_at, updated_at
            ) VALUES (
                'observe-verification', ?, 'observe-scan', 41, 91, 'ebook',
                'Example Author', 'Example Book', '/books/Example Book.epub',
                'fingerprint', ?, 40, 'native-epub', '{}',
                '2026-09-20T14:00:00+00:00', '2026-09-20T14:00:00+00:00'
            )
            """,
            (result_id, verdict),
        )
        conn.commit()
    return original, result_id


def _source_snapshot():
    with local_conn() as conn:
        tables = [
            "scans",
            "scan_results",
            "ebook_acquisitions",
            "ebook_admissions",
            "cleanup_actions",
            "metadata_repairs",
            "content_verifications",
        ]
        return {
            table: [
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            ]
            for table in tables
        }


def test_manual_mode_records_nothing(monkeypatch, tmp_path):
    original, _ = _seed_review_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "manual")
        before = _source_snapshot()

        result = run_observe_cycle()

        after = _source_snapshot()
        with local_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM automation_observations"
            ).fetchone()["n"]
    finally:
        settings.config_dir = original

    assert result["enabled"] is False
    assert result["state"] == "disabled"
    assert result["decisionCount"] == 0
    assert count == 0
    assert after == before


def test_observe_mode_records_decision_without_changing_source_state(
    monkeypatch,
    tmp_path,
):
    original, result_id = _seed_review_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        before = _source_snapshot()

        result = run_observe_cycle()

        after = _source_snapshot()
        snapshot = observe_snapshot()
    finally:
        settings.config_dir = original

    assert after == before
    assert result["enabled"] is True
    assert result["state"] == "observed"
    assert result["decisionCount"] == 1
    record = result["records"][0]
    assert record["subjectKind"] == "result"
    assert int(record["subjectId"]) == result_id
    assert record["decision"] == "attention"
    assert record["reasonCode"] == "INSUFFICIENT_EVIDENCE"
    assert snapshot["count"] == 1
    assert snapshot["items"][0]["id"] == record["id"]


def test_observe_cycle_is_idempotent_for_unchanged_durable_state(monkeypatch, tmp_path):
    original, _ = _seed_review_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")

        first = run_observe_cycle()
        second = run_observe_cycle()

        with local_conn() as conn:
            rows = conn.execute(
                "SELECT id, observed_count FROM automation_observations"
            ).fetchall()
    finally:
        settings.config_dir = original

    assert first["records"][0]["id"] == second["records"][0]["id"]
    assert len(rows) == 1
    assert rows[0]["observed_count"] == 2


def test_new_safe_state_resolves_observe_attention(monkeypatch, tmp_path):
    original, result_id = _seed_review_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        run_observe_cycle()
        assert len(observe_attention_items()) == 1

        with local_conn() as conn:
            conn.execute(
                """
                UPDATE content_verifications
                SET verdict='VERIFIED_CORRECT', confidence=99,
                    updated_at='2026-09-20T14:10:00+00:00'
                WHERE result_id=?
                """,
                (result_id,),
            )
            conn.commit()

        result = run_observe_cycle()
        attention = observe_attention_items()
        with local_conn() as conn:
            decisions = [
                row["decision"]
                for row in conn.execute(
                    """
                    SELECT decision
                    FROM automation_observations
                    WHERE subject_kind='result' AND subject_id=?
                    ORDER BY id
                    """,
                    (str(result_id),),
                ).fetchall()
            ]
    finally:
        settings.config_dir = original

    assert result["records"][0]["decision"] == "no_action"
    assert decisions == ["attention", "no_action"]
    assert attention == []


def test_observe_evidence_is_durable_and_human_readable(monkeypatch, tmp_path):
    original, _ = _seed_review_result(tmp_path, verdict="WRONG_CONTENT")
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")

        result = run_observe_cycle()
        record = result["records"][0]

        with local_conn() as conn:
            stored = conn.execute(
                "SELECT evidence_json FROM automation_observations WHERE id=?",
                (record["id"],),
            ).fetchone()
    finally:
        settings.config_dir = original

    evidence = json.loads(stored["evidence_json"])
    assert record["decision"] == "would_resolve_wrong_content"
    assert record["reasonCode"] == "VERIFIED_WRONG_CONTENT"
    assert evidence["scanId"] == "observe-scan"
    assert evidence["verification"]["verdict"] == "WRONG_CONTENT"
    assert "future Automatic Mode should quarantine" in evidence["nextStep"]



def test_unverified_ebook_is_proposed_verification_not_attention(monkeypatch, tmp_path):
    original, result_id = _seed_review_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        with local_conn() as conn:
            conn.execute(
                "DELETE FROM content_verifications WHERE result_id=?",
                (result_id,),
            )
            conn.commit()

        result = run_observe_cycle()
        attention = observe_attention_items()
    finally:
        settings.config_dir = original

    assert result["decisionCount"] == 1
    record = result["records"][0]
    assert record["decision"] == "would_verify_result"
    assert record["reasonCode"] == "CONTENT_VERIFICATION_REQUIRED"
    assert "future Automatic Mode should run" in record["nextStep"]
    assert attention == []



def test_unverified_audiobook_is_proposed_verification_not_attention(monkeypatch, tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_verification_db()
    try:
        create_scan("audio-observe-scan", 1)
        add_result(
            "audio-observe-scan",
            {
                "file_id": 51,
                "book_id": 101,
                "author": "Example Author",
                "title": "Example Audio Book",
                "format": "audiobook",
                "stored_path": "/data/media/audiobooks/Example Audio Book",
                "local_path": "/audiobooks/Example Audio Book",
                "classification": "REVIEW",
                "risk_score": 80,
                "reason_code": "MISMATCH",
                "reasons": ["fixture"],
                "metadata": {},
            },
        )
        finish_scan("audio-observe-scan")
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")

        result = run_observe_cycle()
        attention = observe_attention_items()
    finally:
        settings.config_dir = original

    assert result["decisionCount"] == 1
    record = result["records"][0]
    assert record["decision"] == "would_verify_audiobook"
    assert record["reasonCode"] == "AUDIOBOOK_VERIFICATION_REQUIRED"
    assert attention == []
