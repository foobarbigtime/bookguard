from app import attention, history
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
