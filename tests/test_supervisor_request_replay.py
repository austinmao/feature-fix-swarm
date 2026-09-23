"""Managed dispatch request keys bind exactly one durable child intent."""
from dataclasses import replace
from pathlib import Path
import sys

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from run_state.ownership import OwnershipRefused
from run_state.supervisor import SupervisorRefused
from test_supervised_process import setup_owner


def test_completed_managed_request_never_spawns_or_debits_twice(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    first = supervisor.launch(request)
    supervisor.finish(first, timeout=10, token_usage=0)

    with pytest.raises(SupervisorRefused, match="REQUEST_ALREADY_COMPLETED"):
        supervisor.launch(request)
    with pytest.raises(OwnershipRefused, match="IDEMPOTENCY_CONFLICT"):
        supervisor.launch(replace(
            request,
            command=(sys.executable, "-c", "raise SystemExit(0)"),
        ))

    retry = supervisor.launch(replace(request, request_key="retry-after-completion"))
    supervisor.finish(retry, timeout=10, token_usage=0)

    with store.read_transaction() as tx:
        intents = tx.execute(
            "SELECT id,attempt_ordinal,state FROM authority_launch_intents "
            "WHERE activity_id=? ORDER BY attempt_ordinal", (request.activity_id,),
        ).fetchall()
        limits = tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()
    assert [(row["id"], row["state"]) for row in intents] == [
        (first.intent_id, "completed_succeeded"),
        (retry.intent_id, "completed_succeeded"),
    ]
    assert [row["attempt_ordinal"] for row in intents] == [1, 2]
    assert tuple(limits) == (2, 0)


def test_reopened_supervisor_refuses_the_same_completed_request(tmp_path):
    from run_state.state import ControlStore
    from run_state.supervisor import Supervisor

    supervisor, store, request = setup_owner(tmp_path)
    first = supervisor.launch(request)
    supervisor.finish(first, timeout=10, token_usage=0)

    resumed = Supervisor(
        ControlStore(store.db_path), supervisor.token, evidence_root=supervisor.evidence_root,
    )
    with pytest.raises(SupervisorRefused, match="REQUEST_ALREADY_COMPLETED"):
        resumed.launch(request)
    with store.read_transaction() as tx:
        assert tx.execute("SELECT count(*) FROM authority_launch_intents").fetchone()[0] == 1
        assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("boundary", "expected_intents", "expected_debits", "expected_refusal"),
    [
        ("reserve_launch.after_write_before_commit", 0, 0, None),
        ("reserve_launch.after_commit_before_return", 1, 1, "INTENT_RECONCILIATION_REQUIRED"),
    ],
)
def test_request_binding_and_debit_share_the_reservation_transaction(
    tmp_path, boundary, expected_intents, expected_debits, expected_refusal,
):
    def fault(point):
        if point == boundary:
            raise RuntimeError(boundary)

    supervisor, store, request = setup_owner(tmp_path)
    store.fault_probe = fault
    with pytest.raises(RuntimeError, match=boundary):
        supervisor.launch(request)
    with store.read_transaction() as tx:
        intents = tx.execute("SELECT count(*) FROM authority_launch_intents").fetchone()[0]
        bindings = tx.execute(
            "SELECT count(*) FROM authority_event_keys WHERE idempotency_key=?",
            ("dispatch-request:" + request.request_key,),
        ).fetchone()[0]
        debits = tx.execute("SELECT count(*) FROM authority_launch_accounting").fetchone()[0]
    assert (intents, bindings, debits) == (expected_intents, expected_debits, expected_debits)

    if expected_refusal:
        with pytest.raises(SupervisorRefused, match=expected_refusal):
            supervisor.launch(request)
        return

    # The rollback erased both mapping and debit, so the same request can be
    # admitted once and executes as a real child.
    store.fault_probe = None
    handle = supervisor.launch(request)
    supervisor.finish(handle, timeout=10, token_usage=0)
