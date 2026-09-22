"""Cohort ACKs retain monitor/native identities for observation-only replay."""
from dataclasses import replace
import sys

from test_supervised_process import setup_owner, _receipt_bound_request
from run_state.supervisor import Supervisor


def test_cohort_monitor_replay_preserves_attempt_and_accounting(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    request = _receipt_bound_request(store, supervisor.token, replace(
        request, monitor_result=True, command=(sys.executable, "-c", "print('retained')"),
    ))
    handle, = supervisor.launch_cohort((request,), request_key="monitored-wave")
    handle.process.wait(timeout=15)
    with store.read_transaction() as tx:
        before = tuple(tx.execute("SELECT generation,acknowledgement_id,permit_id,child_pid FROM authority_launch_intents WHERE id=?",
                                  (handle.intent_id,)).fetchone())
    replay = Supervisor(store, supervisor.token, evidence_root=supervisor.evidence_root)
    observed = replay.resume_monitored(handle.intent_id)
    result = replay.finish(observed, timeout=15, token_usage=0)
    assert result["returncode"] == 0
    assert observed.identity == handle.identity
    assert observed.monitor_identity == handle.monitor_identity
    with store.read_transaction() as tx:
        assert tuple(tx.execute("SELECT generation,acknowledgement_id,permit_id,child_pid FROM authority_launch_intents WHERE id=?",
                                (handle.intent_id,)).fetchone()) == before
        assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 1
