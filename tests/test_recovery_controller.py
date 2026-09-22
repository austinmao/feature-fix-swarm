"""Focused E7 authority-reader regressions; no dispatcher or second database."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from run_state.ownership import ControlStore, ProcessIdentity, StartRequest, reserve_resources
from run_state.recovery_controller import (
    ControlStoreRecoveryAuthority, FrozenRecoveryBinding, RecoveryController,
    RecoveryRefused, RecoveryTrial,
)
from run_state.recovery_docs import CachedDocument


def H(value):
    return value * 64


@pytest.fixture(autouse=True)
def stable_identity(monkeypatch):
    monkeypatch.setattr(ProcessIdentity, "current", classmethod(
        lambda cls: cls("host", "boot", 123, "start")))


def frozen(*, advanced=False):
    return FrozenRecoveryBinding("a" * 40, H("b"), H("c") if advanced else H("d"), H("d"), H("e"), H("f"), H("e"))


def packet(binding, *, stage="EXECUTE"):
    return {"schema": "ffs.frontend-recovery-continuation/v1", "acceptance_hash": binding.acceptance_hash,
            "candidate_hash": binding.candidate_hash, "saved_stage": stage, "failed_criteria": ["criterion"],
            "consumed_attempts": ["final_review"] if stage == "FINAL_REVIEW" else [],
            "remaining_allowances": {"recovery_cycle_normal": 1, "recovery_cycle_autonomous": 2},
            "remaining_launches": 5, "remaining_active_ns": 1, "generation": 1,
            "runtime_hash": binding.runtime_hash, "base_candidate_hash": binding.original_candidate_hash,
            "outstanding_activity_ids": ["activity"], "required_obligation_ids": ["criterion"], "choices": []}


def docs():
    content = b"version-bound bytes"
    return [CachedDocument(content, {"content_sha256": hashlib.sha256(content).hexdigest(), "dependency": "pkg",
                           "version": "1.0", "identity": "pkg@1.0", "source_url": "https://example.test/doc",
                           "provenance": {"source": "approved"}}, Path("receipt"), True)]


def role_receipt(binding, *, role="recovery"):
    return {"schema": "ffs.run-policy-receipt/v1", "role": role, "request_key": "request", "activity_id": "activity",
            "intent_id": "intent", "fence_generation": 1, "acceptance_hash": binding.acceptance_hash,
            "candidate_hash": binding.candidate_hash, "runtime_hash": binding.runtime_hash,
            "workspace_preparation_hash": H("9"), "evidence": [{"id": "terminal", "sha256": H("1"), "locator": "proof"}],
            "completion_status": "succeeded", "process_identity": {"host_id": "host", "boot_id": "boot", "pid": 1, "start_token": "start"},
            "review_dimensions": []}


def authority(tmp_path, *, mode="normal", advanced=False):
    store = ControlStore(tmp_path / "control.sqlite3", policy_clock=lambda: ("boot", 1))
    owner = reserve_resources(store, StartRequest("run", str(tmp_path / "workspace"), "objective", ProcessIdentity.current(),
                                                   repository_id="repository", planning_scope="scope"))
    activity = store.create_activity(owner.token, kind="plan", input_digest=H("0"), retry_budget=1, request_key="root")
    store.bind_runtime(owner.token, activity.id, H("f"))
    store.configure_run_limits(owner.token, dispatch_limit=20, token_limit=1, worker_capacity=2)
    store.configure_run_policy_budget(owner.token, tier="small", clock_boot_id="boot", clock_monotonic_ns=1, recovery_mode=mode)
    binding = frozen(advanced=advanced)
    return store, owner, binding, ControlStoreRecoveryAuthority(store, owner.token, binding)


def test_cycle_comes_from_real_policy_row_and_exact_frozen_input(tmp_path):
    store, owner, binding, reader = authority(tmp_path)
    reservation = store.reserve_policy_action(owner.token, action="recovery_cycle_normal", logical_key="recovery-1",
                                              input_hash=binding.input_hash, recovery_cycle=1)
    cycle = reader.cycle(reservation.id)
    assert (cycle.action_id, cycle.mode, cycle.ordinal) == (reservation.id, "normal", 1)
    forged = store.reserve_policy_action(owner.token, action="check", logical_key="unrelated", input_hash=H("1"))
    with pytest.raises(RecoveryRefused, match="CYCLE_BINDING"):
        reader.cycle(forged.id)


def test_mode_mismatch_and_unbound_reservation_are_refused(tmp_path):
    store, owner, binding, reader = authority(tmp_path, mode="autonomous")
    with pytest.raises(Exception, match="RECOVERY_MODE_CONFLICT"):
        store.reserve_policy_action(owner.token, action="recovery_cycle_normal", logical_key="wrong-mode", input_hash=binding.input_hash)
    with pytest.raises(RecoveryRefused, match="AUTHORITY_MISSING"):
        reader.cycle("not-a-real-policy-action")


def test_retained_cycle_ordinal_does_not_change_when_later_cycle_has_same_timestamp(tmp_path, monkeypatch):
    store, owner, binding, reader = authority(tmp_path, mode='autonomous')
    monkeypatch.setattr(store, '_now', lambda: '2026-09-18T00:00:00Z')
    first = store.reserve_policy_action(owner.token, action='recovery_cycle_autonomous', logical_key='cycle-one',
        input_hash=binding.input_hash, recovery_cycle=1)
    second = store.reserve_policy_action(owner.token, action='recovery_cycle_autonomous', logical_key='cycle-two',
        input_hash=binding.input_hash, recovery_cycle=2)
    assert reader.cycle(first.id).ordinal == 1
    assert reader.cycle(second.id).ordinal == 2


def test_no_arbitrary_verifier_and_advanced_candidate_handback_is_preserved(tmp_path):
    store, owner, binding, reader = authority(tmp_path, advanced=True)
    assert binding.candidate_hash != binding.original_candidate_hash
    controller = RecoveryController(packet(binding), binding, docs(), reader)
    assert controller.handback["base_candidate_hash"] == binding.original_candidate_hash
    with pytest.raises(RecoveryRefused, match="AUTHORITY_REQUIRED"):
        RecoveryController(packet(binding), binding, docs(), object())


def test_diagnosis_rejects_qualification_and_fabricated_role_receipts(tmp_path):
    _store, _owner, binding, reader = authority(tmp_path)
    # Structurally invalid/fabricated material never reaches an authority action.
    with pytest.raises(RecoveryRefused, match="RECEIPT_BINDING"):
        reader.receipt(role_receipt(binding, role="qualification"), cycle=type("C", (), {"ordinal": 1})(), expected_action="diagnosis")
    controller = RecoveryController(packet(binding), binding, docs(), reader)
    with pytest.raises(RecoveryRefused, match="AUTHORITY_MISSING"):
        controller.consume("missing", diagnosis_receipt={"role": "qualification"}, trials=[])


def test_trial_requires_distinct_issued_action_then_fails_closed_without_mapped_check_authority(tmp_path):
    store, owner, binding, reader = authority(tmp_path)
    cycle = store.reserve_policy_action(owner.token, action="recovery_cycle_normal", logical_key="cycle", input_hash=binding.input_hash,
                                       recovery_cycle=1)
    controller = RecoveryController(packet(binding), binding, docs(), reader)
    # An arbitrary trial object cannot cause a patch selection: authority first
    # rejects its fabricated receipt/action rather than trusting supplied bytes.
    with pytest.raises(RecoveryRefused, match="RECEIPT"):
        controller.consume(cycle.id, diagnosis_receipt={"role": "recovery"},
                           trials=[RecoveryTrial("not-issued", {"role": "recovery"})])


def test_final_review_consumption_and_actual_document_bytes_are_required(tmp_path):
    _store, _owner, binding, reader = authority(tmp_path)
    bad = packet(binding, stage="FINAL_REVIEW")
    bad["consumed_attempts"] = []
    with pytest.raises(RecoveryRefused, match="FINAL_REVIEW_CONSUMPTION"):
        RecoveryController(bad, binding, docs(), reader)
    with pytest.raises(RecoveryRefused, match="DOCUMENT_BYTES"):
        RecoveryController(packet(binding), binding, [CachedDocument(b"", {}, Path("x"), True)], reader)
