"""Issuing generations survive real owner replacement; probes cannot overwrite newer intent state."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from process_identity import LIVE, ProcessIdentity
from run_state.ownership import OwnershipRefused, StartRequest, release_owner, reserve_resources
from run_state.state import ControlStore
from test_supervised_process import setup_owner


@contextmanager
def _waiting_child(workspace):
    program = ("from pathlib import Path; import sys; print('WAITING', flush=True); "
               "line=sys.stdin.readline(); "
               "Path('executed').touch() if line.strip() == 'EXECUTE' else None")
    child = subprocess.Popen([sys.executable, "-c", program], cwd=workspace,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "WAITING"
        yield child, ProcessIdentity.from_pid(child.pid)
        assert not (Path(workspace) / "executed").exists()
    finally:
        if child.stdin is not None:
            child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()


def _projection(store, intent_id):
    with store.read_transaction() as tx:
        intent = dict(tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,)).fetchone())
        accounting = [dict(r) for r in tx.execute("SELECT * FROM authority_launch_accounting ORDER BY intent_id")]
        limits = [dict(r) for r in tx.execute("SELECT * FROM authority_run_limits ORDER BY repository_id,run_id")]
        retries = [tuple(r) for r in tx.execute("SELECT id,remaining_retry_budget FROM authority_activities ORDER BY id")]
    return intent, accounting, limits, retries


def _successor(store, old_token):
    with store.read_transaction() as tx:
        parent = tx.execute("SELECT * FROM context_runs WHERE repository_id=? AND run_id=?",
                            (old_token.repository_id, old_token.run_id)).fetchone()
    with store.transaction() as tx:
        release_owner(tx, old_token)
    owner = reserve_resources(store, StartRequest(
        old_token.run_id, parent["workspace"], parent["objective_digest"], ProcessIdentity.current(),
        repository_id=old_token.repository_id, planning_scope=parent["planning_scope"],
    ))
    assert owner.generation > old_token.generation
    assert owner.token.nonce != old_token.nonce
    return owner.token


def _bound_intent(store, token, intent, identity, state):
    acknowledgement = None
    permit = None
    if state != "reserved":
        acknowledgement = store.acknowledge_child(intent.id, token, identity)
    if state == "released_to_execute":
        permit = store.authorize_child(acknowledgement, token)
    return intent, acknowledgement, permit


@pytest.mark.parametrize("state", ["reserved", "acknowledged", "released_to_execute"])
def test_aggregate_no_key_replay_preserves_issuing_generation_and_debit(tmp_path, state):
    supervisor, store, request = setup_owner(tmp_path)
    intent = store.reserve_launch(request.activity_id, supervisor.token, token_reservation=7)
    with _waiting_child(request.workspace) as (_process, identity):
        intent, _ack, _permit = _bound_intent(store, supervisor.token, intent, identity, state)
        successor = _successor(store, supervisor.token)
        before = _projection(store, intent.id)
        replay = store.reserve_launch(request.activity_id, successor, token_reservation=7)
        assert replay.id == intent.id and replay.reused is True
        assert _projection(store, intent.id)[0]["generation"] == supervisor.token.generation
        assert _projection(store, intent.id) == before
        assert before[2][0]["dispatch_used"] == 1
        assert before[2][0]["token_committed"] == 7


@pytest.mark.parametrize("state", ["reserved", "acknowledged", "released_to_execute"])
def test_successor_cannot_ack_or_reack_old_intent(tmp_path, state):
    supervisor, store, request = setup_owner(tmp_path)
    intent = store.reserve_launch(request.activity_id, supervisor.token, token_reservation=7)
    with _waiting_child(request.workspace) as (_process, identity):
        intent, _ack, _permit = _bound_intent(store, supervisor.token, intent, identity, state)
        successor = _successor(store, supervisor.token)
        before = _projection(store, intent.id)
        with pytest.raises(OwnershipRefused) as refused:
            store.acknowledge_child(intent.id, successor, identity)
        assert refused.value.code == "INTENT_RECONCILIATION_REQUIRED"
        assert _projection(store, intent.id) == before


@pytest.mark.parametrize("state", ["acknowledged", "released_to_execute"])
def test_successor_cannot_authorize_or_replay_old_permit(tmp_path, state):
    supervisor, store, request = setup_owner(tmp_path)
    intent = store.reserve_launch(request.activity_id, supervisor.token, token_reservation=7)
    with _waiting_child(request.workspace) as (_process, identity):
        intent, acknowledgement, permit = _bound_intent(store, supervisor.token, intent, identity, state)
        successor = _successor(store, supervisor.token)
        before = _projection(store, intent.id)
        with pytest.raises(OwnershipRefused) as refused:
            store.authorize_child(acknowledgement, successor)
        assert refused.value.code == "INTENT_RECONCILIATION_REQUIRED"
        assert _projection(store, intent.id) == before
        assert before[0]["permit_id"] == (None if permit is None else permit.id)
        assert before[0]["generation"] == supervisor.token.generation


def test_same_generation_ack_and_permit_replays_remain_idempotent(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    intent = store.reserve_launch(request.activity_id, supervisor.token, token_reservation=7)
    with _waiting_child(request.workspace) as (_process, identity):
        intent, acknowledgement, permit = _bound_intent(
            store, supervisor.token, intent, identity, "released_to_execute",
        )
        before = _projection(store, intent.id)
        assert store.acknowledge_child(intent.id, supervisor.token, identity) == acknowledgement
        assert store.authorize_child(acknowledgement, supervisor.token) == permit
        after = _projection(store, intent.id)
        assert after[0]["generation"] == before[0]["generation"]
        assert after[0]["child_pid"] == identity.pid
        assert after[0]["permit_id"] == permit.id
        assert after[1:] == before[1:]


def test_recovery_refuses_stale_probe_after_actual_concurrent_authorization(tmp_path):
    supervisor, store, request = setup_owner(tmp_path)
    intent = store.reserve_launch(request.activity_id, supervisor.token, token_reservation=7)
    with _waiting_child(request.workspace) as (_process, identity):
        intent, acknowledgement, _permit = _bound_intent(
            store, supervisor.token, intent, identity, "acknowledged",
        )
        entered = threading.Event()
        resume = threading.Event()
        errors = []
        results = []
        def probe(observed):
            assert observed == identity
            entered.set()
            assert resume.wait(5), "authorization barrier timed out"
            return LIVE
        recovering = ControlStore(store.db_path, liveness_probe=probe)
        def recover():
            try:
                results.append(recovering.recover_intent(intent.id, supervisor.token))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=recover, daemon=False)
        thread.start()
        try:
            assert entered.wait(5), "recovery never probed the real child"
            permit = store.authorize_child(acknowledgement, supervisor.token)
            before = _projection(store, intent.id)
        finally:
            resume.set()
            thread.join(5)
        assert not thread.is_alive()
        assert results == []
        assert len(errors) == 1 and isinstance(errors[0], OwnershipRefused)
        assert errors[0].code == "INTENT_RECONCILIATION_REQUIRED"
        assert _projection(store, intent.id) == before
        assert before[0]["state"] == "released_to_execute"
        assert before[0]["permit_id"] == permit.id
        assert before[0]["child_pid"] == identity.pid
