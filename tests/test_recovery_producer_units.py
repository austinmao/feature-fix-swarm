"""spec-014 E8 prerequisite 3a: direct unit tests for the shared plumbing the recovery producer rides on.

Each guard here widens a fail-closed gate by exactly one case, so each test pins both sides: the new
case is admitted, and every old refusal still fires.  The assembly fixtures bypass the real role
checks, which is why these are exercised directly.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused
from run_state.supervisor import SupervisorRefused


@pytest.fixture
def _stable_test_identity(monkeypatch):
    """The policy-store tests (not the qualification ones) need no live PID probe."""
    monkeypatch.setattr(ProcessIdentity, "current", classmethod(
        lambda cls: cls("test-host", "test-boot", os.getpid(), "test-start")))


# --- (d) qualification role whitelists --------------------------------------------------------------------

REFUSED_ROLES = ["inventory", "execution", ""]


def test_promotion_admits_the_recovery_role_and_still_refuses_inventory_execution_and_empty(tmp_path):
    from test_qualification_launch_authority import (
        _complete_probes, _publish, _qualification_store, _qualified,
    )
    store, token, workspace_path, contracts, hashes, _envelope = _qualification_store(tmp_path)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    observation = _publish(evidence_root, "qualification-observation.json", {"qualified": True})
    runtime_identity = store.runtime_tuple_hash(_qualified(workspace_path))

    def promote(role):
        return store.promote_qualified_activity(
            token, "inventory-activity", qualification_request_key="qualification-wave",
            expected_contract_hashes=hashes, runtime_identity=runtime_identity,
            final_contract_hash="9" * 64, role=role, observation_evidence=observation)

    for role in REFUSED_ROLES:
        with pytest.raises(OwnershipRefused, match="INVALID_QUALIFICATION_PROMOTION"):
            promote(role)
    _complete_probes(store, token, contracts, evidence_root)
    assert promote("recovery").runtime_tuple_hash == runtime_identity
    with store.read_transaction() as tx:
        assert tx.execute("SELECT role FROM authority_child_bindings WHERE activity_id='inventory-activity'"
                          ).fetchone()[0] == "recovery"
        assert tx.execute("SELECT child_role FROM context_workspaces WHERE preparation_id='preparation'"
                          ).fetchone()[0] == "recovery"


def test_codex_qualification_admits_the_recovery_role_and_still_refuses_inventory_execution_and_empty(
        tmp_path, monkeypatch):
    import run_state.managed_qualification as managed
    from test_managed_qualification import _fixture
    fixture = _fixture(tmp_path)
    store, token, workspace, runtime, binary, gsd, request, evidence, supervisor, module, predicted = fixture
    monkeypatch.setattr(managed, "verify_runtime", lambda *args, **kwargs: predicted)

    def qualify(role):
        return managed.qualify_managed_runtime(
            store, token, activity_id="11111111-1111-4111-8111-111111111111", activity_request_key="plan-key",
            parent_activity_id="parent", workspace=workspace, runtime_home=runtime, binary=binary,
            gsd_environment=gsd, host_request=request, role=role, evidence_root=evidence,
            final_contract_hash="9" * 64, supervisor=supervisor, observer_module=module)

    for role in REFUSED_ROLES:
        with pytest.raises(managed.ManagedQualificationRefused, match="QUALIFICATION_INPUT_INVALID"):
            qualify(role)
    assert supervisor.launched == []
    qualify("recovery")
    assert store.promotions[0][1]["role"] == "recovery"


def test_claude_qualification_admits_the_recovery_role_and_still_refuses_inventory_execution_and_empty(
        tmp_path, monkeypatch):
    import run_state.managed_claude_qualification as managed
    from run_state.host_request import ClaudeHostRequest

    class Passed(Exception):
        pass

    def productive_work(*_args, **_kwargs):
        raise Passed()  # the role guard is the statement before this call

    monkeypatch.setattr(managed, "productive_work", productive_work)
    request = ClaudeHostRequest(str(tmp_path / "candidate"), str(tmp_path / "credential"), str(tmp_path / "claude"),
                                "claude-opus-5", None, "workspace-write", False, 0, 60)
    workspace = SimpleNamespace(parent_activity_id="parent", child_request_key="request", ready=True,
                                path=tmp_path, id="workspace", input_digest="a" * 64, base_commit="b" * 40)

    def qualify(role):
        return managed.qualify_managed_claude_runtime(
            object(), SimpleNamespace(repository_id="repo", run_id="run", generation=1),
            activity_id="11111111-1111-4111-8111-111111111111", activity_request_key="request",
            parent_activity_id="parent", workspace=workspace, host_request=request, role=role,
            evidence_root=tmp_path, final_contract_hash="c" * 64, supervisor=object(), bridge_command="[]")

    for role in REFUSED_ROLES:
        with pytest.raises(managed.ManagedClaudeQualificationRefused, match="QUALIFICATION_INPUT_INVALID"):
            qualify(role)
    with pytest.raises(Passed):
        qualify("recovery")


# --- (e) cancel_unlaunched_policy_review ------------------------------------------------------------------

@pytest.mark.parametrize("action", ["diagnosis", "recovery_trial"])
def test_an_unlaunched_diagnosis_or_trial_reservation_cancels_and_an_issued_one_never_does(
        tmp_path, action, _stable_test_identity):
    from test_run_policy_budget import INPUT_A, INPUT_B, _owned
    store, owner, activity = _owned(tmp_path)
    token = owner.token
    cycle = 1 if action == "recovery_trial" else None
    if cycle:
        store.reserve_policy_action(token, action="recovery_cycle_normal", logical_key="cycle", input_hash=INPUT_A,
                                    recovery_cycle=1)
    stale = store.reserve_policy_action(token, action=action, logical_key="launch", input_hash=INPUT_A,
                                        recovery_cycle=cycle)
    store.cancel_unlaunched_policy_review(token, action_id=stale.id)
    store.cancel_unlaunched_policy_review(token, action_id=stale.id)  # idempotent
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_CANCELLED"):
        store.reserve_policy_action(token, action=action, logical_key="launch", input_hash=INPUT_A,
                                    recovery_cycle=cycle)
    issued = store.reserve_policy_action(token, action=action, logical_key="launch", input_hash=INPUT_B,
                                         recovery_cycle=cycle)
    store.reserve_launch(activity.id, token, policy_action_id=issued.id)
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_ALREADY_ISSUED"):
        store.cancel_unlaunched_policy_review(token, action_id=issued.id)


@pytest.mark.parametrize("action", ["repair", "execute", "check", "recovery_cycle_normal"])
def test_no_other_action_widens_the_cancel_set(tmp_path, action, _stable_test_identity):
    from test_run_policy_budget import INPUT_A, _owned
    store, owner, _activity = _owned(tmp_path)
    cycle = 1 if action == "recovery_cycle_normal" else None
    reserved = store.reserve_policy_action(owner.token, action=action, logical_key="unlaunched", input_hash=INPUT_A,
                                           recovery_cycle=cycle)
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_ALREADY_ISSUED"):
        store.cancel_unlaunched_policy_review(owner.token, action_id=reserved.id)


# --- (b) resumable_outer_completion -----------------------------------------------------------------------

def _resumable(monkeypatch, stage, decision, *, launch_state="completed_succeeded", evidence_valid=True):
    import run_state.frontend_producers as producers
    from run_state.ownership import OwnershipRefused as Refused
    retained = (SimpleNamespace(intent_id="intent"), {"locator": "x", "sha256": "y" * 64})
    monkeypatch.setattr(producers, "_retained_outer_completion", lambda _store, _activity: retained)

    def verified(_evidence):
        if not evidence_valid:
            raise Refused("EVIDENCE_CHANGED")

    state = None if stage is None else SimpleNamespace(stage=stage, decision_json=decision)
    store = SimpleNamespace(get_frontend_policy_state=lambda **_kwargs: state, _verified_evidence=verified)
    token = SimpleNamespace(repository_id="repo", run_id="run")
    return producers.resumable_outer_completion(store, token, "outer", {"state": launch_state})


@pytest.mark.parametrize(("stage", "decision", "expected"), [
    ("FINAL_REVIEW", None, True),                       # unchanged since F51
    ("RECOVER", {"schema": "handback"}, True),          # a retained handback: the producer finishes the cycle
    ("EXECUTE", {"schema": "continuation"}, True),      # a recovery continuation: execution provably happened
    ("EXECUTE", None, False),                           # no continuation: still refuses, as before
    ("SEALED", None, False), ("DONE", None, False), ("NEEDS_DECISION", {"code": "X"}, False), (None, None, False),
])
def test_resumable_outer_completion_admits_exactly_final_review_recover_and_a_recovery_continuation(
        monkeypatch, stage, decision, expected):
    assert _resumable(monkeypatch, stage, decision) is expected


def test_resumable_outer_completion_still_needs_a_settled_launch_and_verifying_evidence(monkeypatch):
    assert _resumable(monkeypatch, "RECOVER", {"schema": "handback"}, launch_state="completed_failed") is False
    assert _resumable(monkeypatch, "RECOVER", {"schema": "handback"}, evidence_valid=False) is False


# --- (c) the Claude session rebinds a retained outer on resume, as the Codex session does ------------------

def test_a_resumed_claude_session_rebinds_the_retained_outer_workspace(tmp_path, monkeypatch):
    import run_state.frontend_producers as producers
    from test_claude_qualification_crash_replay import _session
    prepare, ready, _counts = _session(tmp_path, monkeypatch, launch={"state": "completed_succeeded",
                                                                       "completion_status": "succeeded"})
    monkeypatch.setattr(producers, "resumable_outer_completion", lambda *_args: True)
    rebound, calls = SimpleNamespace(**vars(ready), rebound=True), []

    def rebind_retained_child(store, token, activity_id, preparation_id):
        calls.append((token.run_id, activity_id, preparation_id))
        return rebound

    monkeypatch.setattr(producers, "rebind_retained_child", rebind_retained_child)
    session = prepare()
    assert calls == [("run", session.outer_activity_id, "outer-workspace")]
    assert session.ready is rebound
    with pytest.raises(SupervisorRefused, match="REQUEST_ALREADY_COMPLETED"):
        session.prepare_outer()
    session.close(None, None, None)


def test_a_fresh_claude_session_rebinds_nothing(tmp_path, monkeypatch):
    import run_state.frontend_producers as producers
    from test_claude_qualification_crash_replay import _session
    prepare, ready, _counts = _session(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(producers, "rebind_retained_child", lambda *args: calls.append(args))
    session = prepare()
    assert calls == [] and session.ready is ready
    session.close(None, None, None)


# --- (f) _host_final_text ---------------------------------------------------------------------------------

def _stream(*records) -> bytes:
    return "".join(json.dumps(record) + "\n" for record in records).encode()


CODEX_MESSAGE = {"type": "item.completed", "item": {"type": "agent_message", "text": "diagnosis text"}}


@pytest.mark.parametrize(("host", "records", "text"), [
    ({"schema": "ffs.codex-invocation-receipt/v1"}, [CODEX_MESSAGE], "diagnosis text"),
    ({"schema": "ffs.claude-invocation-receipt/v1"}, [{"type": "result", "result": "claude text"}], "claude text"),
    ({"schema": "ffs.native-review-invocation/v1", "host": "codex", "qualification_scope": "one-completed-review",
      "observation": {}}, [CODEX_MESSAGE], "diagnosis text"),
])
def test_host_final_text_returns_the_single_final_message(host, records, text):
    from run_state.sealed_review import _host_final_text
    assert _host_final_text({"host_receipt": {"status": "complete", **host}}, _stream(*records)) == text


@pytest.mark.parametrize(("host", "records"), [
    ({"schema": "ffs.codex-invocation-receipt/v1", "status": "uncertain"}, [CODEX_MESSAGE]),
    ({"schema": "ffs.codex-invocation-receipt/v1", "status": "complete"}, [CODEX_MESSAGE, CODEX_MESSAGE]),
    ({"schema": "ffs.codex-invocation-receipt/v1", "status": "complete"}, []),
    ({"schema": "ffs.unknown/v1", "status": "complete"}, [CODEX_MESSAGE]),
])
def test_host_final_text_raises_value_error_and_review_output_keeps_its_refusal_code(host, records):
    from run_state.sealed_review import _host_final_text, _review_output
    result, raw = {"host_receipt": host}, _stream(*records)
    with pytest.raises(ValueError):
        _host_final_text(result, raw)
    with pytest.raises(SupervisorRefused) as refused:
        _review_output(result, raw)
    assert refused.value.code == "FINAL_REVIEW_OUTPUT_INVALID"
