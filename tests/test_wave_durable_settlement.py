"""Durable issuer settlement, independent from the consumer's in-memory lock."""

from __future__ import annotations

from test_wave_consumer import wave_fixture


def test_busy_consumer_fence_does_not_abort_outer_settlement(tmp_path, monkeypatch):
    import sys
    import threading
    import time
    from run_state.state import ControlStoreRefused
    from run_state.supervisor import finish_owned_wave_client

    with wave_fixture(tmp_path, monkeypatch, plans=1,
                      outer_command=(sys.executable, "-c", "pass")) as f:
        held = threading.Event()
        failures = []
        busy = []
        original = f.consumer._settled_requests

        def observe(intent_id):
            try:
                return original(intent_id)
            except ControlStoreRefused as error:
                busy.append(error.code)
                raise

        monkeypatch.setattr(f.consumer, "_settled_requests", observe)

        def publish():
            try:
                with f.store.fenced_operation(f.supervisor.token):
                    held.set()
                    time.sleep(2.5)  # Longer than the authority mutex busy bound.
                    f.store.record_event_once(
                        f.supervisor.token, f.outer.activity_id, f"gsd-wave:{f.event}:refused",
                        {"event_id": f.event, "code": "POLICY_STAGE_INFEASIBLE"},
                    )
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=publish)
        wait = f.consumer.wait_for_idle

        def wait_during_integration(**kwargs):
            thread.start()
            assert held.wait(5)
            return wait(**kwargs)

        monkeypatch.setattr(f.consumer, "wait_for_idle", wait_during_integration)
        try:
            result = finish_owned_wave_client(f.supervisor, f.outer, f.consumer, timeout=15)
        finally:
            thread.join(10)
        assert not thread.is_alive() and not failures
        assert busy == ["STORE_BUSY"]
        assert result["returncode"] == 0
        with f.store.read_transaction() as tx:
            row = tx.execute("SELECT state FROM authority_launch_intents WHERE id=?", (f.outer.intent_id,)).fetchone()
            assert row["state"] == "completed_succeeded"


def test_wait_for_idle_observes_recorded_and_queued_requests_not_lock_state(tmp_path, monkeypatch):
    with wave_fixture(tmp_path, monkeypatch, plans=1) as f:
        queued = f.store.record_event_once(
            f.supervisor.token, f.outer.activity_id,
            f"worker-request:{f.outer.intent_id}:gsd-wave:queued", {
                "operation": "gsd-wave-request", "intent_id": f.outer.intent_id,
            },
        )
        # Holding this lock models a durable request recorded before the
        # consumer thread acquires it.  Settlement is journal-based, so it
        # must neither wait for nor mistake that lock as completion.
        assert f.consumer._lock.acquire(blocking=False)
        try:
            f.store.record_event_once(
                f.supervisor.token, f.outer.activity_id, f"gsd-wave:{f.event}:refused",
                {"event_id": f.event, "code": "POLICY_STAGE_INFEASIBLE"},
            )
            try:
                f.consumer.wait_for_idle(intent_id=f.outer.intent_id, timeout=0.02)
            except Exception as error:
                assert getattr(error, "code", None) == "WAVE_SETTLEMENT_TIMEOUT"
            else:
                raise AssertionError("queued durable request was not observed")
            f.store.record_event_once(
                f.supervisor.token, f.outer.activity_id, f"gsd-wave:{queued['id']}:refused",
                {"event_id": queued["id"], "code": "POLICY_STAGE_INFEASIBLE"},
            )
            f.consumer.wait_for_idle(intent_id=f.outer.intent_id, timeout=0.02)
        finally:
            f.consumer._lock.release()
