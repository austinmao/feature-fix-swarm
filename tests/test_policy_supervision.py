"""Policy deadlines and process death through the real supervisor transport."""
from dataclasses import replace
import sys
import time

import pytest

from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused
from run_state.supervisor import Supervisor, SupervisorRefused
from test_supervised_process import setup_owner


def _configured(tmp_path, command):
    supervisor, store, request = setup_owner(tmp_path)
    store.configure_run_policy_budget(
        supervisor.token, tier="small", clock_boot_id=ProcessIdentity.current().boot_id,
        clock_monotonic_ns=time.monotonic_ns(),
    )
    request = supervisor.reserve_request_action(
        replace(request, command=command, monitor_result=True, token_reservation=10), action="execute",
    )
    return supervisor, store, supervisor.launch(request)


def test_stopped_process_unknown_tokens_holds_accounting_but_not_active_time(tmp_path):
    supervisor, store, handle = _configured(tmp_path, (sys.executable, "-c", "print('done')"))
    supervisor.finish(handle, timeout=15)
    budget = store.get_run_policy_budget(repository_id=supervisor.token.repository_id, run_id=supervisor.token.run_id)
    assert budget.launch_charged == 1 and not budget.clock_active
    with store.read_transaction() as tx:
        intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
        limits = tx.execute("SELECT * FROM authority_run_limits").fetchone()
    assert intent["state"] == "uncertain"
    assert limits["token_committed"] == 10 and limits["token_used"] == 0


def test_run_active_deadline_revokes_whole_subtree_before_containment(tmp_path):
    supervisor, store, handle = _configured(tmp_path, (sys.executable, "-c", "import time; time.sleep(180)"))
    # Advance the fixture's already-consumed amount near its immutable limit;
    # the production wait still uses real monotonic time and actual processes.
    with store.transaction() as tx:
        tx.execute("UPDATE authority_run_policy_budgets SET active_ns=active_limit_ns-20000000")
    try:
        with pytest.raises((SupervisorRefused, OwnershipRefused), match="ACTIVE_TIME_EXHAUSTED"):
            supervisor.finish(handle, timeout=15)
        with store.read_transaction() as tx:
            intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (handle.intent_id,)).fetchone()
            parent = tx.execute("SELECT a.state FROM context_runs r JOIN authority_activities a ON a.id=r.activity_id").fetchone()
            limits = tx.execute("SELECT * FROM authority_run_limits").fetchone()
        assert parent["state"] == "failed"
        assert intent["state"] == "reconcile_required" and intent["permit_id"] is None
        assert limits["token_committed"] == 10
        assert handle.process.poll() is not None
    finally:
        if handle.process.poll() is None:
            supervisor.expire_launch(handle, reason="fixture cleanup")


def test_resumed_live_monitor_keeps_original_deadline_and_budget(tmp_path):
    supervisor, store, original = _configured(tmp_path, (sys.executable, "-c", "import time; time.sleep(180)"))
    successor = Supervisor(store, supervisor.token, evidence_root=supervisor.evidence_root)
    observed = successor.resume_monitored(original.intent_id)
    try:
        with pytest.raises(SupervisorRefused, match="CHILD_DEADLINE_EXCEEDED"):
            successor.finish(observed, timeout=.01)
        with store.read_transaction() as tx:
            intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (original.intent_id,)).fetchone()
        assert intent["permit_id"] is None
        assert intent["child_pid"] == original.identity.pid
        assert store.get_run_policy_budget(repository_id=supervisor.token.repository_id,
                                            run_id=supervisor.token.run_id).launch_charged == 1
    finally:
        original.process.wait(timeout=5)


def test_producer_rejects_supplied_action_for_another_class_or_dispatch_input(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    store.configure_run_policy_budget(supervisor.token, tier="small",
                                      clock_boot_id=ProcessIdentity.current().boot_id,
                                      clock_monotonic_ns=time.monotonic_ns())
    bound = supervisor.reserve_request_action(request, action="execute")
    for forged, action in ((bound, "final_review"),
                           (replace(bound, request_key="other-request"), "execute"),
                           (replace(bound, command=(sys.executable, "-c", "print('changed')")), "execute")):
        with pytest.raises(SupervisorRefused, match="POLICY_ACTION_BINDING_CONFLICT"):
            supervisor.reserve_request_action(forged, action=action)
    disguised = supervisor.reserve_request_action(request, action="final_review")
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_BINDING_CONFLICT"):
        supervisor.launch(disguised)
    with store.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0
        assert tx.execute("SELECT COUNT(*) FROM authority_policy_actions").fetchone()[0] == 2
