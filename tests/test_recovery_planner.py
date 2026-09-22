from __future__ import annotations

import json

from app.config import settings
from app.db import add_result, create_scan, finish_scan, init_local_db, local_conn
from app.observe import run_observe_cycle
from app.recovery_planner import (
    block_recovery_plan,
    record_recovery_step_success,
    recovery_plan_by_id,
    recovery_plan_snapshot,
    recovery_plan_transition_snapshot,
)
from app.verifier import init_verification_db


def _seed_result(
    tmp_path,
    *,
    verdict: str = "WRONG_CONTENT",
    reason_code: str = "MIXED_AUDIO_CONTENT",
    source: str = "audiobook-whole-set-evidence",
):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_verification_db()

    create_scan("planner-scan", 1)
    add_result(
        "planner-scan",
        {
            "file_id": 1052,
            "book_id": 6993,
            "author": "James Patterson",
            "title": "1st to Die",
            "format": "audiobook",
            "stored_path": "/audiobooks/James Patterson/1st to Die (2001)",
            "local_path": "/audiobooks/James Patterson/1st to Die (2001)",
            "classification": "REVIEW",
            "risk_score": 95,
            "reason_code": "MISMATCH",
            "reasons": ["fixture"],
            "metadata": {},
        },
    )
    finish_scan("planner-scan")

    with local_conn() as conn:
        result_id = int(
            conn.execute(
                "SELECT id FROM scan_results WHERE scan_id='planner-scan'"
            ).fetchone()["id"]
        )
        evidence = {
            "expected": {
                "title": "1st to Die",
                "author": "James Patterson",
                "mediaKind": "audiobook",
            },
            "reasonCode": reason_code,
            "identity": {
                "wholeSet": {
                    "readableCount": 24,
                    "titleMatchCount": 8,
                    "titleMismatchCount": 16,
                    "mixedContent": reason_code == "MIXED_AUDIO_CONTENT",
                }
            },
        }
        conn.execute(
            """
            INSERT INTO content_verifications(
                signature, result_id, scan_id, file_id, book_id, format,
                author, title, target_path, file_fingerprint, verdict,
                confidence, source, evidence_json, created_at, updated_at
            ) VALUES (
                'planner-verification', ?, 'planner-scan', 1052, 6993,
                'audiobook', 'James Patterson', '1st to Die',
                '/audiobooks/James Patterson/1st to Die (2001)',
                'media-set:fixture', ?, 98, ?, ?,
                '2026-09-21T20:00:00+00:00',
                '2026-09-21T20:00:00+00:00'
            )
            """,
            (result_id, verdict, source, json.dumps(evidence)),
        )
        conn.commit()

    return original, result_id


def test_mixed_wrong_content_creates_non_executable_recovery_plan(monkeypatch, tmp_path):
    original, result_id = _seed_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")

        observed = run_observe_cycle()
        snapshot = recovery_plan_snapshot()
    finally:
        settings.config_dir = original

    assert observed["decisionCount"] == 1
    assert observed["planCount"] == 1
    assert len(observed["plans"]) == 1

    plan = observed["plans"][0]
    assert plan["resultId"] == result_id
    assert plan["planKind"] == "RECOVER_MIXED_AUDIO_CONTENT"
    assert plan["reasonCode"] == "MIXED_AUDIO_CONTENT"
    assert plan["state"] == "planned"
    assert plan["executionAllowed"] is False
    assert plan["preconditions"]["verification"]["verdict"] == "WRONG_CONTENT"
    assert plan["preconditions"]["subject"]["fileId"] == 1052

    codes = [step["code"] for step in plan["steps"]]
    assert codes == [
        "revalidate_whole_set",
        "derive_file_disposition",
        "resolve_proven_associations",
        "quarantine_proven_foreign_media",
        "reacquire_missing_expected_media",
        "verify_replacement_set",
        "reconcile_final_state",
    ]
    assert any(step["externalMutation"] for step in plan["steps"])
    assert snapshot["count"] == 1
    assert snapshot["items"][0]["id"] == plan["id"]


def test_recovery_plan_is_idempotent_for_unchanged_evidence(monkeypatch, tmp_path):
    original, _ = _seed_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")

        first = run_observe_cycle()
        second = run_observe_cycle()

        with local_conn() as conn:
            rows = conn.execute(
                "SELECT id, signature, state FROM recovery_plans ORDER BY id"
            ).fetchall()
    finally:
        settings.config_dir = original

    assert first["plans"][0]["id"] == second["plans"][0]["id"]
    assert len(rows) == 1
    assert rows[0]["state"] == "planned"


def test_attention_supersedes_previously_authorized_plan(monkeypatch, tmp_path):
    original, result_id = _seed_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        first = run_observe_cycle()
        assert first["planCount"] == 1

        with local_conn() as conn:
            conn.execute(
                """
                UPDATE content_verifications
                SET verdict='INSUFFICIENT_EVIDENCE',
                    confidence=40,
                    evidence_json=?,
                    updated_at='2026-09-21T20:05:00+00:00'
                WHERE result_id=?
                """,
                (
                    json.dumps(
                        {
                            "reasonCode": "PARTIAL_MATCH",
                            "explanation": "Evidence became ambiguous.",
                        }
                    ),
                    result_id,
                ),
            )
            conn.commit()

        second = run_observe_cycle()
        with local_conn() as conn:
            row = conn.execute(
                "SELECT state, last_error FROM recovery_plans ORDER BY id DESC LIMIT 1"
            ).fetchone()
    finally:
        settings.config_dir = original

    assert second["records"][0]["decision"] == "attention"
    assert second["planCount"] == 0
    assert row["state"] == "superseded"
    assert "no longer authorizes" in row["last_error"]


def test_verified_correct_supersedes_old_plan(monkeypatch, tmp_path):
    original, result_id = _seed_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        run_observe_cycle()

        with local_conn() as conn:
            conn.execute(
                """
                UPDATE content_verifications
                SET verdict='VERIFIED_CORRECT',
                    confidence=99,
                    evidence_json=?,
                    updated_at='2026-09-21T20:06:00+00:00'
                WHERE result_id=?
                """,
                (
                    json.dumps({"reasonCode": "AUDIOBOOK_IDENTITY_VERIFIED"}),
                    result_id,
                ),
            )
            conn.commit()

        observed = run_observe_cycle()
        snapshot = recovery_plan_snapshot()
    finally:
        settings.config_dir = original

    assert observed["records"][0]["decision"] == "no_action"
    assert observed["planCount"] == 0
    assert snapshot["items"][0]["state"] == "superseded"


def test_manual_mode_does_not_create_recovery_plan(monkeypatch, tmp_path):
    original, _ = _seed_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "manual")
        observed = run_observe_cycle()
        snapshot = recovery_plan_snapshot()
    finally:
        settings.config_dir = original

    assert observed["enabled"] is False
    assert observed["planCount"] == 0
    assert observed["plans"] == []
    assert snapshot["count"] == 0



def test_recovery_step_progress_is_durable_and_replay_idempotent(monkeypatch, tmp_path):
    original, _ = _seed_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        observed = run_observe_cycle()
        plan_id = observed["plans"][0]["id"]
        step_count = len(observed["plans"][0]["steps"])

        advanced = record_recovery_step_success(
            plan_id,
            0,
            now="2026-09-22T02:00:00+00:00",
        )
        assert advanced["state"] == "ready"
        assert advanced["currentStep"] == 1
        assert advanced["finalOutcome"] == ""

        # Simulate a process restart reopening the same durable database.
        init_local_db()
        restored = recovery_plan_by_id(plan_id)
        assert restored is not None
        assert restored["state"] == "ready"
        assert restored["currentStep"] == 1

        before_replay = recovery_plan_transition_snapshot(plan_id)
        replay = record_recovery_step_success(
            plan_id,
            0,
            now="2026-09-22T02:00:05+00:00",
        )
        after_replay = recovery_plan_transition_snapshot(plan_id)
        assert replay["currentStep"] == 1
        assert after_replay == before_replay

        for step_index in range(1, step_count):
            completed = record_recovery_step_success(
                plan_id,
                step_index,
                now=f"2026-09-22T02:{step_index:02d}:00+00:00",
            )

        assert completed["state"] == "completed"
        assert completed["currentStep"] == step_count
        assert completed["finalOutcome"] == "completed"
        assert completed["completedAt"] is not None

        transitions = recovery_plan_transition_snapshot(plan_id)
        assert transitions[0]["event"] == "created"
        step_events = [item for item in transitions if item["event"] == "step_completed"]
        assert len(step_events) == step_count
        assert step_events[-1]["toState"] == "completed"
        assert step_events[-1]["toStep"] == step_count
    finally:
        settings.config_dir = original


def test_blocked_recovery_plan_is_durable_and_cannot_advance(monkeypatch, tmp_path):
    original, _ = _seed_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        observed = run_observe_cycle()
        plan_id = observed["plans"][0]["id"]

        blocked = block_recovery_plan(
            plan_id,
            "Mutation-boundary evidence changed.",
            now="2026-09-22T03:00:00+00:00",
        )
        assert blocked["state"] == "blocked"
        assert blocked["finalOutcome"] == "blocked"
        assert "evidence changed" in blocked["lastError"]

        init_local_db()
        restored = recovery_plan_by_id(plan_id)
        assert restored is not None
        assert restored["state"] == "blocked"
        assert restored["finalOutcome"] == "blocked"

        transitions = recovery_plan_transition_snapshot(plan_id)
        assert transitions[-1]["event"] == "blocked"
        assert transitions[-1]["toState"] == "blocked"
        assert "evidence changed" in transitions[-1]["detail"]

        import pytest
        with pytest.raises(RuntimeError, match="cannot advance"):
            record_recovery_step_success(
                plan_id,
                0,
                now="2026-09-22T03:00:05+00:00",
            )
    finally:
        settings.config_dir = original



def test_new_evidence_revision_audits_replaced_plan(monkeypatch, tmp_path):
    original, result_id = _seed_result(tmp_path)
    try:
        monkeypatch.setenv("BOOKGUARD_AUTOMATION_MODE", "observe")
        first = run_observe_cycle()
        old_plan_id = first["plans"][0]["id"]

        with local_conn() as conn:
            conn.execute(
                """
                UPDATE content_verifications
                SET evidence_json=?,
                    updated_at='2026-09-22T04:00:00+00:00'
                WHERE result_id=?
                """,
                (
                    json.dumps(
                        {
                            "expected": {
                                "title": "1st to Die",
                                "author": "James Patterson",
                                "mediaKind": "audiobook",
                            },
                            "reasonCode": "MUSIC_MISMATCH",
                        }
                    ),
                    result_id,
                ),
            )
            conn.commit()

        second = run_observe_cycle()
        new_plan = second["plans"][0]
        old_plan = recovery_plan_by_id(old_plan_id)
        old_transitions = recovery_plan_transition_snapshot(old_plan_id)
        new_transitions = recovery_plan_transition_snapshot(new_plan["id"])
    finally:
        settings.config_dir = original

    assert new_plan["id"] != old_plan_id
    assert new_plan["planKind"] == "RECOVER_NONBOOK_AUDIO"
    assert old_plan is not None
    assert old_plan["state"] == "superseded"
    assert old_plan["finalOutcome"] == "superseded"
    assert old_transitions[-1]["event"] == "superseded"
    assert "newer evidence revision" in old_transitions[-1]["detail"]
    assert new_transitions[0]["event"] == "created"
