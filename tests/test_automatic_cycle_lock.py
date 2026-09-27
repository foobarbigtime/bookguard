from __future__ import annotations

import threading

import pytest

import app.automatic_runner as runner


def test_overlapping_automatic_cycles_are_refused(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_cycle(limit):
        calls.append(limit)
        entered.set()
        assert release.wait(5)
        return {"ok": True, "state": "executed"}

    monkeypatch.setattr(runner, "_run_automatic_cycle_locked", slow_cycle)
    results = []
    first = threading.Thread(target=lambda: results.append(runner.run_automatic_cycle()))
    first.start()
    try:
        assert entered.wait(5)
        with pytest.raises(runner.AutomaticExecutionBlocked) as blocked:
            runner.run_automatic_cycle()
        assert blocked.value.reason_code == "AUTOMATIC_CYCLE_RUNNING"
    finally:
        release.set()
        first.join(5)

    assert calls == [100]
    assert results == [{"ok": True, "state": "executed"}]


def test_cycle_lock_is_released_after_a_failed_cycle(monkeypatch):
    def failing_cycle(_limit):
        raise runner.AutomaticExecutionBlocked("AUTOMATIC_MODE_INACTIVE", "inactive")

    monkeypatch.setattr(runner, "_run_automatic_cycle_locked", failing_cycle)
    for _ in range(2):
        with pytest.raises(runner.AutomaticExecutionBlocked) as blocked:
            runner.run_automatic_cycle()
        assert blocked.value.reason_code == "AUTOMATIC_MODE_INACTIVE"
    assert not runner._automatic_cycle_lock.locked()
