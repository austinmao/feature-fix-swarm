"""Atomic managed launch cohorts retain one durable accounting decision."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused
from run_state.state import ControlStore, LaunchCohortRequest
from test_runtime_receipt_authority import INPUT_SHA, _managed_store, _qualified


def _cohort_store(tmp_path):
    store, token, workspace = _managed_store(tmp_path)
    with store.transaction() as tx:
        tx.execute(
            "UPDATE authority_activities SET runtime_tuple_hash=? WHERE id='activity'",
            ("b" * 64,),
        )
        tx.execute(
            "INSERT INTO authority_activities "
            "(id,repository_id,run_id,kind,input_digest,revision,state,retry_budget,"
            "remaining_retry_budget,runtime_tuple_hash,request_key,generation,created_at,updated_at) "
            "VALUES('activity-two','repository','run','execute',?,2,'active',2,2,?,"
            "'activity-two-request',?,'now','now')",
            (INPUT_SHA, "b" * 64, token.generation),
        )

    def validate(_tx, _token, activity, receipt_sha256, managed_input_sha256,
                 _principal, _now):
        if receipt_sha256 == "0" * 64:
            raise OwnershipRefused("RUNTIME_RECEIPT_INVALID")
        if managed_input_sha256 != activity["input_digest"]:
            raise OwnershipRefused("MANAGED_INPUT_MISMATCH")

    store._validate_runtime_receipt_tx = validate
    return store, token, workspace


def _members(*, bad_second=False, payload_two=None):
    return (
        LaunchCohortRequest(
            "activity", "child-one", {"command_sha256": "1" * 64}, 5,
            "1" * 64, INPUT_SHA,
        ),
        LaunchCohortRequest(
            "activity-two", "child-two",
            payload_two or {"command_sha256": "2" * 64}, 7,
            ("0" if bad_second else "2") * 64, INPUT_SHA,
        ),
    )


def test_cohort_member_uses_real_runtime_receipt_and_input_hash(tmp_path):
    store, token, workspace = _managed_store(tmp_path)
    receipt = store.commit_runtime_receipt(token, "activity", _qualified(workspace))
    request = LaunchCohortRequest(
        "activity", "real-child", {"command_sha256": "9" * 64}, 7,
        receipt.receipt_sha256, INPUT_SHA,
    )
    cohort = store.reserve_launch_cohort(
        token, request_key="real-wave", members=[request],
    )
    assert len(cohort.members) == 1
    with store.read_transaction() as tx:
        member = tx.execute(
            "SELECT runtime_receipt_sha256,managed_input_sha256 "
            "FROM authority_launch_cohort_members"
        ).fetchone()
    assert tuple(member) == (receipt.receipt_sha256, INPUT_SHA)


def test_cohort_reservation_is_atomic_replayable_and_restart_durable(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    first = store.reserve_launch_cohort(
        token, request_key="wave-one", members=_members(),
    )
    assert len(first.members) == 2
    assert first.state == "reserved"
    with store.read_transaction() as tx:
        limits = tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()
        attempts = tx.execute(
            "SELECT remaining_retry_budget FROM authority_activities ORDER BY revision"
        ).fetchall()
    assert tuple(limits) == (2, 12)
    assert [row[0] for row in attempts] == [1, 1]

    replay = ControlStore(store.db_path).reserve_launch_cohort(
        token, request_key="wave-one", members=reversed(_members()),
    )
    assert replay.reused
    assert [item.id for item in replay.members] == [item.id for item in first.members]

    with pytest.raises(OwnershipRefused, match="COHORT_MEMBERSHIP_CONFLICT"):
        store.reserve_launch_cohort(
            token, request_key="wave-one",
            members=_members(payload_two={"command_sha256": "3" * 64}),
        )
    with store.read_transaction() as tx:
        assert tuple(tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()) == (2, 12)


def test_malformed_member_rolls_back_every_debit_and_retry(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    with pytest.raises(OwnershipRefused, match="RUNTIME_RECEIPT_INVALID"):
        store.reserve_launch_cohort(
            token, request_key="bad-wave", members=_members(bad_second=True),
        )
    with store.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_cohorts").fetchone()[0] == 0
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0
        assert tuple(tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()) == (0, 0)
        assert [row[0] for row in tx.execute(
            "SELECT remaining_retry_budget FROM authority_activities ORDER BY revision"
        ).fetchall()] == [2, 2]


def test_concurrent_exact_cohort_reservation_debits_once(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    barrier = threading.Barrier(2)
    results = []

    def reserve():
        barrier.wait()
        results.append(store.reserve_launch_cohort(
            token, request_key="concurrent-wave", members=_members(),
        ))

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({result.id for result in results}) == 1
    assert sorted(result.reused for result in results) == [False, True]
    with store.read_transaction() as tx:
        assert tuple(tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()) == (2, 12)
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 2


def test_release_requires_every_exact_ack_and_is_atomic(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    cohort = store.reserve_launch_cohort(
        token, request_key="release-wave", members=_members(),
    )
    children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                for _ in range(2)]
    try:
        identities = [ProcessIdentity.from_pid(child.pid) for child in children]
        store.acknowledge_cohort_child(
            cohort.id, cohort.members[0].id, token, identities[0],
        )
        with pytest.raises(OwnershipRefused, match="COHORT_ACK_INCOMPLETE"):
            store.release_launch_cohort(cohort.id, token)
        with pytest.raises(OwnershipRefused, match="COHORT_RECONCILIATION_REQUIRED"):
            store.reserve_launch(
                "activity", token, token_reservation=5, request_key="child-one",
                request_payload={"command_sha256": "1" * 64},
                runtime_receipt_sha256="1" * 64, managed_input_sha256=INPUT_SHA,
            )
        store.acknowledge_cohort_child(
            cohort.id, cohort.members[1].id, token, identities[1],
        )
        permits = store.release_launch_cohort(cohort.id, token)
        assert [permit.intent_id for permit in permits] == [item.id for item in cohort.members]
        assert store.authorize_launch_cohort(cohort.id, token) == permits
        with store.read_transaction() as tx:
            states = tx.execute(
                "SELECT state FROM authority_launch_intents ORDER BY activity_id"
            ).fetchall()
            retained = tx.execute(
                "SELECT state FROM authority_launch_cohorts WHERE id=?", (cohort.id,),
            ).fetchone()[0]
        assert {row[0] for row in states} == {"released_to_execute"}
        assert retained == "released_to_execute"
    finally:
        for child in children:
            child.terminate()
        for child in children:
            child.wait(timeout=10)


def test_pre_release_crash_reconciles_without_refund_or_relaunch(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    cohort = store.reserve_launch_cohort(
        token, request_key="crash-wave", members=_members(),
    )
    reopened = ControlStore(store.db_path)
    reconciled = reopened.reconcile_launch_cohort(cohort.id, token)
    assert reconciled.state == "reconcile_required"
    replay = reopened.reserve_launch_cohort(
        token, request_key="crash-wave", members=_members(),
    )
    assert replay.reused
    assert [member.id for member in replay.members] == [member.id for member in cohort.members]
    with pytest.raises(OwnershipRefused, match="COHORT_RECONCILIATION_REQUIRED"):
        reopened.reserve_launch(
            "activity", token, token_reservation=5, request_key="different-launch",
            request_payload={}, runtime_receipt_sha256="1" * 64,
            managed_input_sha256=INPUT_SHA,
        )
    with reopened.read_transaction() as tx:
        assert tuple(tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()) == (2, 12)
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 2


def test_stale_owner_fence_cannot_replay_or_release_cohort(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    cohort = store.reserve_launch_cohort(
        token, request_key="fenced-wave", members=_members(),
    )
    stale = replace(token, nonce="stale-owner")
    with pytest.raises(OwnershipRefused, match="FENCE_REVOKED"):
        store.reserve_launch_cohort(
            stale, request_key="fenced-wave", members=_members(),
        )
    with pytest.raises(OwnershipRefused, match="FENCE_REVOKED"):
        store.release_launch_cohort(cohort.id, stale)
    with store.read_transaction() as tx:
        assert tuple(tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()) == (2, 12)


def test_failed_member_refuses_whole_cohort_release(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    cohort = store.reserve_launch_cohort(
        token, request_key="failed-wave", members=_members(),
    )
    with store.transaction() as tx:
        tx.execute(
            "UPDATE authority_launch_intents SET state='completed_failed',"
            "completion_status='failed' WHERE id=?", (cohort.members[0].id,),
        )
    with pytest.raises(OwnershipRefused, match="COHORT_MEMBER_FAILED"):
        store.release_launch_cohort(cohort.id, token)
    with store.read_transaction() as tx:
        assert tx.execute(
            "SELECT state FROM authority_launch_cohorts WHERE id=?", (cohort.id,),
        ).fetchone()[0] == "reserved"
        assert tuple(tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()) == (2, 12)


def test_tampered_retained_session_identity_refuses_release(tmp_path):
    store, token, _workspace = _cohort_store(tmp_path)
    cohort = store.reserve_launch_cohort(
        token, request_key="identity-wave", members=_members(),
    )
    children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                for _ in range(2)]
    try:
        for member, child in zip(cohort.members, children):
            store.acknowledge_cohort_child(
                cohort.id, member.id, token, ProcessIdentity.from_pid(child.pid),
            )
        with store.transaction() as tx:
            tx.execute(
                "UPDATE authority_launch_cohort_members SET child_start_token='reused' "
                "WHERE cohort_id=? AND intent_id=?", (cohort.id, cohort.members[0].id),
            )
        with pytest.raises(OwnershipRefused, match="CHILD_IDENTITY_MISMATCH"):
            store.release_launch_cohort(cohort.id, token)
        with store.read_transaction() as tx:
            assert {row[0] for row in tx.execute(
                "SELECT state FROM authority_launch_intents"
            ).fetchall()} == {"acknowledged"}
    finally:
        for child in children:
            child.terminate()
        for child in children:
            child.wait(timeout=10)
