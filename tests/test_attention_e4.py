from datetime import datetime, timedelta, timezone

from app import attention, attention_execution, history
from app.config import settings
from app.db import init_local_db, local_conn, utc_now


def test_blocked_e4_plan_is_visible_and_auditable_without_work(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    monkeypatch.setattr(attention, "acquisition_coordinator_status", lambda: {"state": "disabled"})
    monkeypatch.setattr(attention, "observe_attention_items", lambda _: [])
    init_local_db()
    now = utc_now()
    with local_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO recovery_plans(
                 signature, planner_version, subject_kind, subject_id,
                 title, author, path, plan_kind, reason_code, state,
                 evidence_revision, preconditions_json, steps_json,
                 created_at, updated_at, last_error
               ) VALUES (
                 'attention-fixture', 'E3', 'admission', '47',
                 'Fixture', 'Fixture Author', '/data/media/books/Fixture.epub',
                 'REVIEW_ADMISSION_PREPUBLICATION',
                 'ADMISSION_FAILED_BEFORE_PUBLICATION', 'blocked',
                 'revision', '{}', '[]', ?, ?, ?
               )""",
            (now, now, "The staged source changed; mutation refused."),
        )
        plan_id = cursor.lastrowid
        conn.commit()

    snapshot = attention.attention_snapshot()
    item = next(row for row in snapshot["items"] if row["kind"] == "recovery_plan")
    assert snapshot["summary"]["recoveryPlans"] == 1
    assert item["id"] == plan_id
    assert item["status"] == "blocked"
    assert "staged source changed" in item["message"]
    assert item["detailHref"] == f"/history/recovery-plan/{plan_id}"
    detail = history.operation_detail("recovery-plan", plan_id)
    assert detail and "execution outcomes are recorded separately" in detail["summary"]

    with local_conn() as conn:
        assert conn.execute(
            "SELECT state FROM recovery_plans WHERE id=?", (plan_id,),
        ).fetchone()["state"] == "blocked"
        conn.execute("UPDATE recovery_plans SET state='superseded' WHERE id=?", (plan_id,))
        conn.commit()
    assert attention.attention_snapshot()["summary"]["recoveryPlans"] == 0


def test_only_stale_running_e4_receipts_enter_attention(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    monkeypatch.setattr(attention, "acquisition_coordinator_status", lambda: {"state": "disabled"})
    monkeypatch.setattr(attention, "observe_attention_items", lambda _: [])
    init_local_db()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=20)).isoformat()
    recent = (now - timedelta(minutes=1)).isoformat()
    with local_conn() as conn:
        plan = conn.execute(
            """INSERT INTO recovery_plans(
                 signature, planner_version, subject_kind, subject_id,
                 title, author, path, plan_kind, reason_code, state,
                 evidence_revision, preconditions_json, steps_json,
                 created_at, updated_at
               ) VALUES ('running-fixture', 'E4', 'admission', '47',
                 'Fixture', 'Fixture Author', '/data/media/books/Fixture.epub',
                 'REVIEW_ADMISSION_PREPUBLICATION',
                 'ADMISSION_FAILED_BEFORE_PUBLICATION', 'ready',
                 'revision', '{}', '[]', ?, ?)""",
            (old, old),
        ).lastrowid
        for index, (code, timestamp) in enumerate((
            ("retire_proven_prepublication_failure", old),
            ("admit_verified_acquisition", recent),
        )):
            conn.execute(
                """INSERT INTO automatic_executions(
                     plan_id, plan_signature, action_code, step_index,
                     state, evidence_revision, created_at, updated_at
                   ) VALUES (?, 'running-fixture', ?, ?, 'running',
                     'revision', ?, ?)""",
                (plan, code, index, timestamp, timestamp),
            )
        conn.commit()

    items = attention_execution.stale_execution_items(now=now)
    assert len(items) == 1
    assert items[0]["kind"] == "automatic_execution"
    assert items[0]["detailHref"] == f"/history/recovery-plan/{plan}"
    assert "outcome is unproven" in items[0]["message"]
    assert attention.attention_snapshot()["summary"]["runningExecutions"] == 1

    with local_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM automatic_executions WHERE state='running'"
        ).fetchone()[0] == 2
        conn.execute(
            "UPDATE automatic_executions SET state='succeeded' WHERE updated_at=?", (old,),
        )
        conn.commit()
    assert attention_execution.stale_execution_items(now=now) == []
