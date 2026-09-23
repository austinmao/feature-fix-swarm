"""Focused E4 authority regressions; no frontend or second-store harness."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os

import pytest

from run_state.ownership import ControlStore, OwnershipRefused, ProcessIdentity, StartRequest, reserve_resources


INPUT_A = "a" * 64
INPUT_B = "b" * 64
RUNTIME = "c" * 64


def test_overlapping_local_work_uses_union_and_idle_gap_is_free(tmp_path):
    store, owner, _ = _owned(tmp_path)
    first = store.begin_policy_work(owner.token, kind="preparation", clock_boot_id="boot-a", clock_monotonic_ns=150)
    second = store.begin_policy_work(owner.token, kind="check", clock_boot_id="boot-a", clock_monotonic_ns=200)
    store.end_policy_work(owner.token, first, clock_boot_id="boot-a", clock_monotonic_ns=250)
    store.end_policy_work(owner.token, second, clock_boot_id="boot-a", clock_monotonic_ns=300)
    third = store.begin_policy_work(owner.token, kind="harvest", clock_boot_id="boot-a", clock_monotonic_ns=500)
    store.end_policy_work(owner.token, third, clock_boot_id="boot-a", clock_monotonic_ns=550)
    budget = store.get_run_policy_budget(repository_id=owner.token.repository_id, run_id=owner.token.run_id)
    assert budget.active_ns == 200
    assert not budget.clock_active


def test_local_work_exhaustion_is_retained_and_blocks_new_work(tmp_path):
    store, owner, _ = _owned(tmp_path)
    first = store.begin_policy_work(owner.token, kind="check", clock_boot_id="boot-a", clock_monotonic_ns=100)
    limit = 60 * 60 * 1_000_000_000
    store.end_policy_work(owner.token, first, clock_boot_id="boot-a", clock_monotonic_ns=limit + 101)
    with pytest.raises(OwnershipRefused, match="ACTIVE_TIME_EXHAUSTED"):
        store.begin_policy_work(owner.token, kind="check", clock_boot_id="boot-a", clock_monotonic_ns=limit + 102)
    budget = store.get_run_policy_budget(repository_id=owner.token.repository_id, run_id=owner.token.run_id)
    assert budget.active_ns == limit + 1
    assert not budget.clock_active


def test_idle_boot_change_reanchors_but_unproven_active_interval_cannot_be_zeroed(tmp_path):
    store, owner, _ = _owned(tmp_path)
    store.policy_clock = lambda: ("boot-b", 5)
    store.reserve_policy_action(owner.token, action="check", logical_key="after-idle-reboot", input_hash=INPUT_A)
    budget = store.get_run_policy_budget(repository_id=owner.token.repository_id, run_id=owner.token.run_id)
    assert budget.clock_boot_id == "boot-b" and budget.active_ns == 0 and not budget.clock_uncertain
    store.begin_policy_work(owner.token, kind="check")
    store.policy_clock = lambda: ("boot-c", 2)
    with pytest.raises(OwnershipRefused, match="CLOCK_RECONCILIATION_REQUIRED"):
        store.reserve_policy_action(owner.token, action="check", logical_key="after-active-reboot", input_hash=INPUT_A)
    with pytest.raises(OwnershipRefused, match="POLICY_CLOCK_INTERVAL_UNPROVEN"):
        store.reconcile_run_policy_clock(owner.token, receipt=_clock_receipt(
            tmp_path, old_boot="boot-b", old_ns=5, new_boot="boot-c", new_ns=2, duration=0))
    assert store.get_run_policy_budget(repository_id=owner.token.repository_id, run_id=owner.token.run_id).clock_uncertain


@pytest.mark.parametrize("files,loc,protected,expected", [
    (4, 199, False, "small"), (5, 199, False, "medium"), (4, 200, False, "medium"),
    (20, 1500, False, "medium"), (21, 1, False, "large"), (1, 1501, False, "large"),
    (1, 1, True, "large"),
])
def test_frozen_ceremony_thresholds(files, loc, protected, expected):
    from run_state.run_policy import classify_ceremony
    assert classify_ceremony({"files": files, "loc": loc, "protected": protected}) == expected


@pytest.fixture(autouse=True)
def _stable_test_identity(monkeypatch):
    """The host fixture is intentionally synthetic; policy tests need no PID probe."""
    monkeypatch.setattr(ProcessIdentity, "current", classmethod(
        lambda cls: cls("test-host", "test-boot", os.getpid(), "test-start"),
    ))


def _owned(tmp_path, *, retries=4, recovery_mode="normal"):
    store = ControlStore(tmp_path / "authority" / "control.sqlite3", policy_clock=lambda: ("boot-a", 100))
    owner = reserve_resources(store, StartRequest(
        "budget-run", str(tmp_path / "workspace"), "objective", ProcessIdentity.current(),
        repository_id="budget-repository", planning_scope="budget-scope",
    ))
    activity = store.create_activity(owner.token, kind="plan", input_digest=INPUT_A,
                                     retry_budget=retries, request_key="budget-activity")
    store.bind_runtime(owner.token, activity.id, RUNTIME)
    store.configure_run_limits(owner.token, dispatch_limit=30, token_limit=100, worker_capacity=30)
    store.configure_run_policy_budget(owner.token, tier="small", clock_boot_id="boot-a",
                                      clock_monotonic_ns=100, recovery_mode=recovery_mode)
    return store, owner, activity


def _clock_receipt(tmp_path, *, old_boot="boot-a", old_ns=100, new_boot="boot-b", new_ns=5, duration=0):
    evidence_path = tmp_path / "clock-reconciliation-evidence.txt"
    receipt = {
        "schema": "ffs.run-policy-clock-reconciliation/v1",
        "old_boot_id": old_boot,
        "old_monotonic_ns": old_ns,
        "new_boot_id": new_boot,
        "new_monotonic_ns": new_ns,
        "prior_interval_ns": duration,
    }
    evidence_path.write_text(json.dumps({**receipt, "repository_id": "budget-repository", "run_id": "budget-run"}))
    return {**receipt, "evidence": {"locator": str(evidence_path), "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()}}


def test_concurrent_final_review_reservation_is_one_durable_grant(tmp_path):
    store, owner, _activity = _owned(tmp_path)

    def reserve(key):
        return store.reserve_policy_action(owner.token, action="final_review", logical_key=key,
                                           input_hash=INPUT_A)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, key) for key in ("review-a", "review-b")]
        results = [future.exception() for future in futures]
    assert sum(error is None for error in results) == 1
    assert sum(isinstance(error, OwnershipRefused) for error in results) == 1
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_LIMIT_EXHAUSTED"):
        store.reserve_policy_action(owner.token, action="final_review", logical_key="review-c", input_hash=INPUT_B)


def test_capacity_poll_reuses_issued_action_and_only_unlaunched_review_can_cancel(tmp_path):
    store, owner, _activity = _owned(tmp_path)
    repair = store.reserve_policy_action(owner.token, action="repair", logical_key="repair", input_hash=INPUT_A)
    assert store.reserve_policy_action(owner.token, action="repair", logical_key="repair", input_hash=INPUT_A).id == repair.id
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_ALREADY_ISSUED"):
        store.cancel_unlaunched_policy_review(owner.token, action_id=repair.id)
    review = store.reserve_policy_action(owner.token, action="final_review", logical_key="draft-review", input_hash=INPUT_A)
    store.cancel_unlaunched_policy_review(owner.token, action_id=review.id)
    assert store.reserve_policy_action(owner.token, action="final_review", logical_key="ready-review", input_hash=INPUT_B).id != review.id
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_CANCELLED"):
        store.reserve_policy_action(owner.token, action="final_review", logical_key="draft-review", input_hash=INPUT_A)


def test_rejected_or_no_patch_launch_remains_charged_and_bound(tmp_path):
    store, owner, activity = _owned(tmp_path)
    repair = store.reserve_policy_action(owner.token, action="repair", logical_key="repair-1", input_hash=INPUT_A)
    intent = store.reserve_launch(activity.id, owner.token, policy_action_id=repair.id)
    budget = store.get_run_policy_budget(repository_id=owner.token.repository_id, run_id=owner.token.run_id)
    assert intent.id and budget and budget.launch_charged == 1
    # No completion/refund is possible merely because this attempt produced no patch.
    assert store.get_run_policy_budget(repository_id=owner.token.repository_id, run_id=owner.token.run_id).launch_charged == 1


def test_review_receipt_dedup_and_new_input_cannot_reuse_broad_grant(tmp_path):
    store, owner, _activity = _owned(tmp_path)
    review = store.reserve_policy_action(owner.token, action="final_review", logical_key="final", input_hash=INPUT_A)
    # A malformed first transport is not a clean review and leaves retry
    # settlement available; its launch debit is made at reserve_launch.
    assert store.record_policy_action_receipt(owner.token, action_id=review.id, input_hash=INPUT_A,
                                              receipt_hash="e" * 64, valid=False).receipt_hash is None
    receipt = store.record_policy_action_receipt(owner.token, action_id=review.id, input_hash=INPUT_A,
                                                  receipt_hash="d" * 64, valid=True)
    assert receipt.receipt_hash == "d" * 64
    assert store.record_policy_action_receipt(owner.token, action_id=review.id, input_hash=INPUT_A,
                                              receipt_hash="d" * 64, valid=True).reused
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_LIMIT_EXHAUSTED"):
        store.reserve_policy_action(owner.token, action="final_review", logical_key="final", input_hash=INPUT_B)


def test_recovery_trials_and_stage_feasibility_are_bounded(tmp_path):
    store, owner, _activity = _owned(tmp_path)
    cycle = store.reserve_policy_action(owner.token, action="recovery_cycle_normal", logical_key="cycle-1", input_hash=INPUT_A,
                                        recovery_cycle=1)
    assert cycle.recovery_cycle == 1
    with pytest.raises(OwnershipRefused, match="RECOVERY_MODE_CONFLICT"):
        store.reserve_policy_action(owner.token, action="recovery_cycle_autonomous", logical_key="cycle-2", input_hash=INPUT_A,
                                    recovery_cycle=2)
    for ordinal in range(3):
        store.reserve_policy_action(owner.token, action="recovery_trial", logical_key=f"trial-{ordinal}", input_hash=INPUT_A,
                                    recovery_cycle=1)
    with pytest.raises(OwnershipRefused, match="RECOVERY_TRIAL_LIMIT_EXHAUSTED"):
        store.reserve_policy_action(owner.token, action="recovery_trial", logical_key="trial-4", input_hash=INPUT_A,
                                    recovery_cycle=1)
    with pytest.raises(OwnershipRefused, match="POLICY_STAGE_INFEASIBLE"):
        store.reserve_policy_action(owner.token, action="repair", logical_key="needs-13", input_hash=INPUT_A,
                                    required_launch_overhead=13)


def test_clock_mismatch_persists_fence_and_requires_bound_reconciliation_receipt(tmp_path):
    store, owner, _activity = _owned(tmp_path)
    with pytest.raises(OwnershipRefused, match="CLOCK_RECONCILIATION_REQUIRED"):
        store.record_policy_wait(owner.token, kind="operator", elapsed_ns=1,
                                 clock_boot_id="wrong-boot", clock_monotonic_ns=101)
    assert store.get_run_policy_budget(repository_id=owner.token.repository_id, run_id=owner.token.run_id).clock_uncertain
    with pytest.raises(OwnershipRefused, match="CLOCK_RECONCILIATION_REQUIRED"):
        store.reserve_policy_action(owner.token, action="execute", logical_key="blocked", input_hash=INPUT_A)
    with pytest.raises(OwnershipRefused, match="CLOCK_RECONCILIATION_REQUIRED"):
        store.record_policy_wait(owner.token, kind="operator", elapsed_ns=1,
                                 clock_boot_id="boot-a", clock_monotonic_ns=101)
    with pytest.raises(OwnershipRefused, match="POLICY_CLOCK_RECEIPT_BINDING_INVALID"):
        store.reconcile_run_policy_clock(owner.token, receipt=_clock_receipt(tmp_path, old_boot="wrong-boot"))
    reconciled = store.reconcile_run_policy_clock(owner.token, receipt=_clock_receipt(tmp_path))
    assert not reconciled.clock_uncertain and reconciled.clock_boot_id == "boot-b"
    store.policy_clock = lambda: ("boot-b", 5)
    assert store.reserve_policy_action(owner.token, action="execute", logical_key="allowed", input_hash=INPUT_A).mutation_allowed


def test_actual_intent_statuses_keep_union_clock_for_unknown_work_not_queues(tmp_path):
    store, owner, activity = _owned(tmp_path)
    execute = store.reserve_policy_action(owner.token, action="execute", logical_key="work", input_hash=INPUT_A)
    intent = store.reserve_launch(activity.id, owner.token, policy_action_id=execute.id)
    with store.transaction() as tx:
        tx.execute("UPDATE authority_launch_intents SET state='uncertain',child_host_id='host',child_boot_id='boot',child_pid=7,child_start_token='start' WHERE id=?", (intent.id,))
        assert store._policy_has_productive_work_tx(tx, owner.token)
        # An identity-bearing unknown child starts one union interval; another
        # sibling would not multiply this boolean interval.
        assert store._policy_clock_tx(tx, owner.token, boot_id="boot-a", monotonic_ns=150, active_after=True)
        # A second queued/reserved child does not add a second active interval.
        tx.execute("UPDATE authority_launch_intents SET state='reserved',child_host_id=NULL,child_boot_id=NULL,child_pid=NULL,child_start_token=NULL WHERE id=?", (intent.id,))
        assert not store._policy_has_productive_work_tx(tx, owner.token)
        assert store._policy_clock_tx(tx, owner.token, boot_id="boot-a", monotonic_ns=200, active_after=False)
        # Queue-only time after proven no live/unknown identity is excluded.
        assert store._policy_clock_tx(tx, owner.token, boot_id="boot-a", monotonic_ns=250, active_after=False)
        clock = tx.execute("SELECT active_ns,clock_active FROM authority_run_policy_budgets WHERE repository_id=? AND run_id=?",
                           (owner.token.repository_id, owner.token.run_id)).fetchone()
        assert (clock["active_ns"], clock["clock_active"]) == (50, 0)
        tx.execute("UPDATE authority_launch_intents SET state='reconcile_required',child_host_id='host',child_boot_id='boot',child_pid=7,child_start_token='start' WHERE id=?", (intent.id,))
        assert store._policy_has_productive_work_tx(tx, owner.token)


def test_policy_vocabulary_mode_and_late_configuration_refusal(tmp_path):
    store, owner, activity = _owned(tmp_path)
    assert not store.reserve_policy_action(owner.token, action="check", logical_key="check", input_hash=INPUT_A).mutation_allowed
    assert not store.reserve_policy_action(owner.token, action="diagnosis", logical_key="diagnosis", input_hash=INPUT_A).mutation_allowed
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_INVALID"):
        store.reserve_policy_action(owner.token, action="unknown", logical_key="unknown", input_hash=INPUT_A)
    mode_store, mode_owner, _mode_activity = _owned(tmp_path / "autonomous", recovery_mode="autonomous")
    for ordinal in (1, 2):
        mode_store.reserve_policy_action(mode_owner.token, action="recovery_cycle_autonomous",
                                         logical_key=f"cycle-{ordinal}", input_hash=INPUT_A,
                                         recovery_cycle=ordinal)
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_LIMIT_EXHAUSTED"):
        mode_store.reserve_policy_action(mode_owner.token, action="recovery_cycle_autonomous",
                                         logical_key="cycle-3", input_hash=INPUT_A, recovery_cycle=3)
    with pytest.raises(OwnershipRefused, match="RECOVERY_MODE_CONFLICT"):
        mode_store.reserve_policy_action(mode_owner.token, action="recovery_cycle_normal",
                                         logical_key="wrong-mode", input_hash=INPUT_A, recovery_cycle=1)
    with pytest.raises(OwnershipRefused, match="RUN_POLICY_IMMUTABLE"):
        mode_store.configure_run_policy_budget(mode_owner.token, tier="medium", clock_boot_id="boot-a",
                                               clock_monotonic_ns=100, recovery_mode="autonomous")
    autonomous_store = ControlStore(tmp_path / "late" / "control.sqlite3")
    late_owner = reserve_resources(autonomous_store, StartRequest(
        "late-run", str(tmp_path / "late-workspace"), "objective", ProcessIdentity.current(),
        repository_id="late-repository", planning_scope="late-scope",
    ))
    late_activity = autonomous_store.create_activity(late_owner.token, kind="plan", input_digest=INPUT_A,
                                                     retry_budget=2, request_key="late-activity")
    autonomous_store.bind_runtime(late_owner.token, late_activity.id, RUNTIME)
    autonomous_store.configure_run_limits(late_owner.token, dispatch_limit=3, token_limit=0, worker_capacity=3)
    autonomous_store.reserve_launch(late_activity.id, late_owner.token)
    with pytest.raises(OwnershipRefused, match="POLICY_LATE_CONFIGURATION_REFUSED"):
        autonomous_store.configure_run_policy_budget(late_owner.token, tier="small", clock_boot_id="boot-a", clock_monotonic_ns=1)


def test_legacy_run_without_policy_is_unaffected(tmp_path):
    store = ControlStore(tmp_path / "authority" / "control.sqlite3")
    owner = reserve_resources(store, StartRequest(
        "legacy-run", str(tmp_path / "legacy"), "objective", ProcessIdentity.current(),
        repository_id="legacy-repository", planning_scope="legacy-scope",
    ))
    activity = store.create_activity(owner.token, kind="plan", input_digest=INPUT_A,
                                     retry_budget=1, request_key="legacy-activity")
    store.bind_runtime(owner.token, activity.id, RUNTIME)
    store.configure_run_limits(owner.token, dispatch_limit=1, token_limit=0, worker_capacity=1)
    assert store.reserve_launch(activity.id, owner.token).id
