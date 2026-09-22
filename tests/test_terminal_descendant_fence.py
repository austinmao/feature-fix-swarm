"""Terminal authority revokes descendants without erasing accounting."""
from dataclasses import replace
import sys

import pytest

from test_supervised_process import setup_owner
from run_state.ownership import OwnershipRefused
from run_state.supervisor import _publish
from run_state.supervisor import SupervisorRefused


def test_terminal_parent_revokes_live_descendant_and_rejects_late_success(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    request = replace(request, command=(sys.executable, "-c", "import time; time.sleep(30)"),
                      token_reservation=10)
    handle = supervisor.launch(request)
    try:
        with store.read_transaction() as tx:
            parent = tx.execute("SELECT parent_activity_id FROM authority_child_bindings WHERE activity_id=?",
                                (request.activity_id,)).fetchone()[0]
            before = tuple(tx.execute("SELECT dispatch_used,token_committed FROM authority_run_limits").fetchone())
        activity = store.get_activity(parent)
        store.transition_activity(supervisor.token, parent, expected=activity.state,
                                  new="failed", reason="acceptance failed")
        with store.read_transaction() as tx:
            row = tx.execute("SELECT state,permit_id,child_pid FROM authority_launch_intents WHERE id=?",
                             (handle.intent_id,)).fetchone()
            assert tuple(row) == ("reconcile_required", None, handle.identity.pid)
            assert tuple(tx.execute("SELECT dispatch_used,token_committed FROM authority_run_limits").fetchone()) == before
    finally:
        handle.process.terminate()
        handle.process.wait(timeout=10)
    evidence = _publish(tmp_path, "late.json", {"finished": True})
    with pytest.raises(OwnershipRefused, match="CHILD_NOT_AUTHORIZED"):
        store.complete_launch(handle.intent_id, supervisor.token, status="succeeded",
                              evidence=evidence, token_usage=0)
    settled = store.complete_launch(handle.intent_id, supervisor.token, status="failed",
                                    evidence=evidence, token_usage=0)
    assert settled.state == "completed_failed"


def test_process_timeout_revokes_and_contains_before_returning(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    request = replace(request, command=(sys.executable, "-c", "import time; time.sleep(30)"),
                      token_reservation=10, monitor_result=True)
    handle = supervisor.launch(request)
    with pytest.raises(SupervisorRefused, match="CHILD_DEADLINE_EXCEEDED"):
        supervisor.finish(handle, timeout=.01)
    assert handle.process.poll() is not None
    with store.read_transaction() as tx:
        intent = tx.execute("SELECT state,permit_id FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
        assert tuple(intent) == ("reconcile_required", None)
        assert tx.execute("SELECT token_committed FROM authority_run_limits").fetchone()[0] == 10
        event = tx.execute("SELECT payload FROM control_events WHERE event_type LIKE 'containment:%'").fetchone()
        assert event is not None
    assert store.get_activity(handle.activity_id).state == "failed"
