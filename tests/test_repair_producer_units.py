"""spec-014 E8 prerequisite 3b: direct unit tests for the shared plumbing the repair producer rides on.

Each guard here widens a fail-closed gate by exactly one case, so each test pins both sides: the new case is
admitted, and every old refusal still fires.  Fixture-level proof only: not native host qualification, not E8.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused


@pytest.fixture
def _stable_test_identity(monkeypatch):
    monkeypatch.setattr(ProcessIdentity, "current", classmethod(
        lambda cls: cls("test-host", "test-boot", os.getpid(), "test-start")))


# --- the release of a stale, never-launched repair grant -----------------------------------------------------

def test_an_unlaunched_repair_grant_cancels_and_an_issued_one_never_does(tmp_path, _stable_test_identity):
    from test_run_policy_budget import INPUT_A, _owned
    store, owner, activity = _owned(tmp_path)
    token = owner.token
    stale = store.reserve_policy_action(token, action="repair", logical_key="launch", input_hash=INPUT_A)
    store.cancel_unlaunched_repair(token, action_id=stale.id)
    store.cancel_unlaunched_repair(token, action_id=stale.id)  # idempotent
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_CANCELLED"):
        store.reserve_policy_action(token, action="repair", logical_key="launch", input_hash=INPUT_A)
    issued = store.reserve_policy_action(token, action="repair", logical_key="issued", input_hash=INPUT_A)
    store.reserve_launch(activity.id, token, policy_action_id=issued.id)
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_ALREADY_ISSUED"):
        store.cancel_unlaunched_repair(token, action_id=issued.id)


@pytest.mark.parametrize("action", ["execute", "check", "final_review", "spec_review"])
def test_the_repair_release_never_cancels_any_other_action(tmp_path, action, _stable_test_identity):
    from test_run_policy_budget import INPUT_A, _owned
    store, owner, _activity = _owned(tmp_path)
    reserved = store.reserve_policy_action(owner.token, action=action, logical_key="unlaunched", input_hash=INPUT_A)
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_ALREADY_ISSUED"):
        store.cancel_unlaunched_repair(owner.token, action_id=reserved.id)


def test_the_review_release_still_refuses_a_repair(tmp_path, _stable_test_identity):
    """The review/diagnosis/trial release is unchanged: a repair is released only through its own method."""
    from test_run_policy_budget import INPUT_A, _owned
    store, owner, _activity = _owned(tmp_path)
    reserved = store.reserve_policy_action(owner.token, action="repair", logical_key="unlaunched", input_hash=INPUT_A)
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_ALREADY_ISSUED"):
        store.cancel_unlaunched_policy_review(owner.token, action_id=reserved.id)


# --- resumable_outer_completion ------------------------------------------------------------------------------

CURRENT = "c" * 64


def _resumable(monkeypatch, stage, decision, *, repaired):
    import run_state.frontend_producers as producers
    retained = (SimpleNamespace(intent_id="intent"), {"locator": "x", "sha256": "y" * 64})
    monkeypatch.setattr(producers, "_retained_outer_completion", lambda _store, _activity: retained)
    monkeypatch.setattr(producers, "_repair_proof", lambda _store, _token, _state: repaired)
    state = None if stage is None else SimpleNamespace(stage=stage, decision_json=decision, candidate_hash=CURRENT)
    store = SimpleNamespace(get_frontend_policy_state=lambda **_kwargs: state,
                            _verified_evidence=lambda _evidence: None)
    token = SimpleNamespace(repository_id="repo", run_id="run")
    return producers.resumable_outer_completion(store, token, "outer", {"state": "completed_succeeded"})


@pytest.mark.parametrize(("stage", "decision", "repaired", "expected"), [
    ("EXECUTE", None, True, True),                      # an issued repair, or failed checks, prove the execution happened
    ("EXECUTE", {"schema": "unrelated"}, True, True),
    ("EXECUTE", None, False, False),                    # a bare EXECUTE proves nothing: still refused
    ("EXECUTE", {"schema": "unrelated"}, False, False),
    ("SEALED", None, True, False),                      # the repair proof never widens any other stage
    ("DONE", None, True, False), ("NEEDS_DECISION", {"code": "X"}, True, False), (None, None, True, False),
], ids=["execute-repair-proof", "execute-other-decision-repair-proof", "execute-no-proof", "execute-other-no-proof",
        "sealed", "done", "needs-decision", "no-state"])
def test_resumable_outer_completion_admits_execute_only_with_a_repair_proof(
        monkeypatch, stage, decision, repaired, expected):
    assert _resumable(monkeypatch, stage, decision, repaired=repaired) is expected


def test_the_repair_proof_is_never_consulted_outside_execute(monkeypatch):
    import run_state.frontend_producers as producers
    asked = []
    retained = (SimpleNamespace(intent_id="intent"), {"locator": "x", "sha256": "y" * 64})
    monkeypatch.setattr(producers, "_retained_outer_completion", lambda _store, _activity: retained)
    monkeypatch.setattr(producers, "_repair_proof", lambda *_args: asked.append(1) or True)
    for stage in ("FINAL_REVIEW", "RECOVER", "SEALED", "DONE"):
        state = SimpleNamespace(stage=stage, decision_json=None, candidate_hash=CURRENT)
        store = SimpleNamespace(get_frontend_policy_state=lambda **_kwargs: state,
                                _verified_evidence=lambda _evidence: None)
        producers.resumable_outer_completion(store, SimpleNamespace(repository_id="r", run_id="x"), "outer",
                                             {"state": "completed_succeeded"})
    assert asked == []
