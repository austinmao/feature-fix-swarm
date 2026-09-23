"""Worker ceiling revisions are future-admission policy, never new allowance."""
from dataclasses import replace

import pytest

import process_identity
from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused, StartRequest, release_owner, reserve_resources
from test_launch_cohort_authority import _cohort_store, _members
from test_qualification_launch_authority import _qualification_store, _reserve


@pytest.fixture(autouse=True)
def _portable_process_start_token(monkeypatch):
    """The fixture authority needs a precise identity on filelock variants."""
    monkeypatch.setattr(process_identity, "process_start_token", lambda pid: str(pid))


def _ordinary(store, token, activity_id, request_key):
    return store.reserve_launch(
        activity_id, token, token_reservation=3, request_key=request_key,
        request_payload={"command_sha256": "a" * 64},
        runtime_receipt_sha256="b" * 64, managed_input_sha256="a" * 64,
    )


def _mark_acknowledged(store, intent_id):
    identity = ProcessIdentity.current()
    with store.transaction() as tx:
        tx.execute(
            "UPDATE authority_launch_intents SET state='acknowledged',child_host_id=?,"
            "child_boot_id=?,child_pid=?,child_start_token=? WHERE id=?",
            (identity.host_id, identity.boot_id, identity.pid, identity.start_token, intent_id),
        )


def test_decrease_drains_active_then_increase_admits_without_resetting_allowances(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    active = _ordinary(store, token, "activity", "first")
    _mark_acknowledged(store, active.id)
    first = store.revise_capacity_policy(
        token, worker_capacity=1, expected_revision=0, request_key="ceiling-one",
    )
    assert first.revision == 1
    with store.read_transaction() as tx:
        assert tx.execute("SELECT state FROM authority_launch_intents WHERE id=?", (active.id,)).fetchone()[0] == "acknowledged"
    with pytest.raises(OwnershipRefused, match="WORKER_CAPACITY_EXHAUSTED"):
        _ordinary(store, token, "activity-two", "blocked")
    # The cohort path uses the same future-admission ceiling and does not
    # disturb the pre-paid ordinary reservation.
    with pytest.raises(OwnershipRefused, match="WORKER_CAPACITY_EXHAUSTED"):
        store.reserve_launch_cohort(token, request_key="blocked-cohort", members=_members())
    second = store.revise_capacity_policy(
        token, worker_capacity=2, expected_revision=1, request_key="ceiling-two",
    )
    assert second.revision == 2
    admitted = _ordinary(store, token, "activity-two", "after-increase")
    assert admitted.id
    with store.read_transaction() as tx:
        # Capacity policy does not modify immutable or cumulative fields.
        limits = tx.execute(
            "SELECT dispatch_limit,token_limit,worker_capacity,dispatch_used,token_committed "
            "FROM authority_run_limits"
        ).fetchone()
    assert tuple(limits) == (4, 100, 2, 2, 6)
    assert store.get_worker_capacity_revision(
        repository_id=token.repository_id, run_id=token.run_id,
    ) == second


def test_qualification_admission_observes_current_ceiling(tmp_path):
    store, token, _workspace, contracts, _hashes, _envelope = _qualification_store(tmp_path)
    # Qualification intents are capacity-exempt themselves, but their new
    # admission still consults the shared ceiling against active workers.
    with store.transaction() as tx:
        identity = ProcessIdentity.current()
        tx.execute(
            "INSERT INTO authority_launch_intents "
            "(id,activity_id,attempt_ordinal,state,generation,capacity_exempt,child_host_id,"
            "child_boot_id,child_pid,child_start_token,created_at,updated_at) "
            "VALUES('active-worker','activity',1,'acknowledged',?,0,?,?,?,?, 'now','now')",
            (token.generation, identity.host_id, identity.boot_id, identity.pid, identity.start_token),
        )
    store.revise_capacity_policy(
        token, worker_capacity=1, expected_revision=0, request_key="qualification-ceiling",
    )
    with pytest.raises(OwnershipRefused, match="WORKER_CAPACITY_EXHAUSTED"):
        _reserve(store, token, contracts["ordinary"])


def test_revision_cas_and_idempotency_are_exact(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    assert store.get_capacity_policy(
        repository_id=token.repository_id, run_id=token.run_id,
    ) == {
        "repository_id": "repository", "run_id": "run", "revision": 0,
        "effective_worker_capacity": 2, "worker_capacity": 2,
    }
    first = store.revise_capacity_policy(
        token, worker_capacity=1, expected_revision=0, request_key="one",
    )
    assert store.revise_capacity_policy(
        token, worker_capacity=1, expected_revision=0, request_key="one",
    ) == first
    with pytest.raises(OwnershipRefused, match="IDEMPOTENCY_CONFLICT"):
        store.revise_capacity_policy(
            token, worker_capacity=2, expected_revision=0, request_key="one",
        )
    with pytest.raises(OwnershipRefused, match="WORKER_CAPACITY_REVISION_CONFLICT"):
        store.revise_capacity_policy(
            token, worker_capacity=2, expected_revision=0, request_key="stale",
        )
    with pytest.raises(OwnershipRefused, match="INVALID_WORKER_CAPACITY_REVISION"):
        store.revise_capacity_policy(
            token, worker_capacity=0, expected_revision=1, request_key="invalid",
        )


def test_current_successor_revises_history_without_rewriting_it_or_limits(tmp_path):
    store, token, workspace = _cohort_store(tmp_path)
    first = store.revise_capacity_policy(
        token, worker_capacity=1, expected_revision=0, request_key="original-owner",
    )
    with store.transaction() as tx:
        release_owner(tx, token)
    successor = reserve_resources(store, StartRequest(
        "run", str(workspace), "objective", ProcessIdentity.current(),
        repository_id="repository", planning_scope="scope",
    )).token
    second = store.revise_capacity_policy(
        successor, worker_capacity=2, expected_revision=1, request_key="successor",
    )
    assert second.owner_generation == successor.generation
    assert [item.owner_generation for item in store.list_worker_capacity_revisions(
        repository_id="repository", run_id="run",
    )] == [first.owner_generation, successor.generation]
    with pytest.raises(OwnershipRefused, match="RUN_LIMITS_IMMUTABLE"):
        store.configure_run_limits(successor, dispatch_limit=5, token_limit=100, worker_capacity=2)
    with store.read_transaction() as tx:
        assert tuple(tx.execute(
            "SELECT dispatch_limit,token_limit,worker_capacity,dispatch_used,token_committed "
            "FROM authority_run_limits"
        ).fetchone()) == (4, 100, 2, 0, 0)
    with pytest.raises(OwnershipRefused, match="FENCE_REVOKED"):
        store.revise_capacity_policy(
            replace(token, nonce="stale"), worker_capacity=3,
            expected_revision=2, request_key="stale-owner",
        )
