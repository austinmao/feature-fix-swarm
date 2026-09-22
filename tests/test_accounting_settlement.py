"""An uncertain meter must not allow a live child or recovery to free capacity."""
from dataclasses import replace
import subprocess
import sys

import pytest

from test_supervised_process import setup_owner
from run_state.ownership import OwnershipRefused
from run_state.state import ControlStore
from run_state.supervisor import _publish
from process_identity import ProcessIdentity


def test_uncertain_settlement_rechecks_actual_child_death(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    request = replace(request, command=(sys.executable, "-c", "import time; time.sleep(30)"),
                      token_reservation=10)
    child = supervisor.launch(request)
    receipt = _publish(child.stdout_path.parent, "meter-uncertain.json", {"meter": "unavailable"})
    try:
        store.complete_launch(child.intent_id, supervisor.token, status="uncertain",
                              evidence=receipt, token_usage=None)
        with pytest.raises(OwnershipRefused, match="CHILD_NOT_TERMINAL"):
            store.complete_launch(child.intent_id, supervisor.token, status="failed",
                                  evidence=receipt, token_usage=0)
        with store.read_transaction() as tx:
            limits = tx.execute("SELECT dispatch_used,token_committed FROM authority_run_limits").fetchone()
        assert tuple(limits) == (1, 10)
    finally:
        child.process.terminate()
        child.process.wait(timeout=10)
    reopened = ControlStore(store.db_path)
    assert reopened.recover_intent(child.intent_id, supervisor.token).state == "uncertain"
    final = reopened.complete_launch(child.intent_id, supervisor.token, status="failed",
                                     evidence=receipt, token_usage=0)
    assert final.state == "completed_failed"
    assert reopened.recover_intent(child.intent_id, supervisor.token).state == "completed_failed"


def test_uncertain_completion_cannot_manufacture_authorization(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    intent = store.reserve_launch(request.activity_id, supervisor.token)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        store.acknowledge_child(intent.id, supervisor.token, ProcessIdentity.from_pid(child.pid))
    finally:
        child.terminate()
        child.wait(timeout=10)
    receipt = _publish(tmp_path, "unreleased-receipt.json", {"meter": "unavailable"})
    store.complete_launch(intent.id, supervisor.token, status="uncertain",
                          evidence=receipt, token_usage=None)
    with pytest.raises(OwnershipRefused, match="CHILD_NOT_AUTHORIZED"):
        store.complete_launch(intent.id, supervisor.token, status="succeeded",
                              evidence=receipt, token_usage=0)
