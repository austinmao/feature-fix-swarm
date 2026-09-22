"""Managed runtime qualification has a bounded pre-receipt launch authority."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from process_identity import ProcessIdentity
from run_state.ownership import OwnershipRefused
from run_state.state import ControlStore
from run_state.supervisor import _publish
from run_state.workspace import begin_child_workspace_preparation, prepare_workspace
from test_child_workspace_recovery import _invoke
from test_registered_child_execution import git
from test_runtime_receipt_authority import INPUT_SHA, _managed_store, _qualified


PROBES = ("ordinary", "native-positive", "native-negative", "native-multi-agent")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _contracts(workspace: Path, *, cohort="qualification-wave"):
    probe_contracts = {
        name: {
            "probe_name": name,
            "command_sha256": hashlib.sha256((name + "-command").encode()).hexdigest(),
            "environment_sha256": hashlib.sha256((name + "-environment").encode()).hexdigest(),
            "qualification_request_id": f"{cohort}:{name}",
        }
        for name in PROBES
    }
    probe_hashes = {
        name: hashlib.sha256(_canonical(contract).encode()).hexdigest()
        for name, contract in probe_contracts.items()
    }
    envelope = {
        "schema": "ffs.qualification-envelope/v1",
        "qualification_cohort_id": cohort,
        "probes": [
            {"probe_name": name, "probe_contract_sha256": probe_hashes[name]}
            for name in PROBES
        ],
        "runtime_template_sha256": "3" * 64,
        "workspace_binding": str(workspace),
        "candidate_input_sha256": INPUT_SHA,
        "model": "fixture-model",
        "effort": "high",
        "sandbox": "workspace-write",
        "roots": [str(workspace)],
        "policy_sha256": "9" * 64,
    }
    envelope_sha256 = hashlib.sha256(_canonical(envelope).encode()).hexdigest()
    contracts = {
        name: {
            "schema": "ffs.qualification-launch/v1",
            "probe_contract": probe_contracts[name],
            "qualification_envelope": envelope,
            "qualification_envelope_sha256": envelope_sha256,
        }
        for name in PROBES
    }
    return contracts, probe_hashes, envelope_sha256


def _qualification_store(tmp_path, *, dispatch_limit=4):
    store, token, workspace = _managed_store(tmp_path)
    contracts, hashes, envelope_sha256 = _contracts(workspace)
    with store.transaction() as tx:
        tx.execute(
            "UPDATE authority_run_limits SET dispatch_limit=?", (dispatch_limit,),
        )
        tx.execute(
            "UPDATE context_workspaces SET child_role='inventory' "
            "WHERE preparation_id='preparation'",
        )
        tx.execute(
            "INSERT INTO authority_activities "
            "(id,repository_id,run_id,kind,input_digest,revision,state,retry_budget,"
            "remaining_retry_budget,runtime_tuple_hash,request_key,generation,created_at,updated_at) "
            "VALUES('inventory-activity','repository','run','execute',?,2,'active',4,4,?,"
            "'inventory-activity-request',?,'now','now')",
            (INPUT_SHA, envelope_sha256, token.generation),
        )
        tx.execute(
            "INSERT INTO authority_child_bindings "
            "(activity_id,parent_activity_id,role,candidate_hash,contract_hash,runtime_identity,"
            "workspace_binding,workspace_preparation_id,created_at) "
            "VALUES('inventory-activity','activity','inventory',?,?,?,?,'preparation','now')",
            (INPUT_SHA, "5" * 64, envelope_sha256, str(workspace)),
        )
    return store, token, workspace, contracts, hashes, envelope_sha256


def _reserve(store, token, contract, *, tokens=7):
    request_key = contract["probe_contract"]["qualification_request_id"]
    return store.reserve_qualification_launch(
        "inventory-activity", token, request_key=request_key,
        qualification_contract=contract, token_reservation=tokens,
        managed_input_sha256=INPUT_SHA,
    )


def test_policy_qualification_grant_binds_exact_closed_probe_contract(tmp_path):
    import time
    from process_identity import ProcessIdentity
    store, token, _workspace, contracts, hashes, _envelope = _qualification_store(tmp_path)
    store.configure_run_policy_budget(token, tier="small", clock_boot_id=ProcessIdentity.current().boot_id,
                                      clock_monotonic_ns=time.monotonic_ns())
    name = PROBES[0]
    contract = contracts[name]
    key = contract["probe_contract"]["qualification_request_id"]
    wrong = store.reserve_policy_action(token, action="qualification", logical_key=key, input_hash="e" * 64)
    with pytest.raises(OwnershipRefused, match="POLICY_ACTION_BINDING_CONFLICT"):
        store.reserve_qualification_launch("inventory-activity", token, request_key=key,
                                          qualification_contract=contract, token_reservation=7,
                                          managed_input_sha256=INPUT_SHA, policy_action_id=wrong.id)
    correct = store.reserve_policy_action(token, action="qualification", logical_key=key, input_hash=hashes[name])
    intent = store.reserve_qualification_launch("inventory-activity", token, request_key=key,
                                               qualification_contract=contract, token_reservation=7,
                                               managed_input_sha256=INPUT_SHA, policy_action_id=correct.id)
    assert intent.id
    assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == 1


def test_qualification_reservation_restarts_replays_and_rejects_conflicts(tmp_path):
    store, token, _workspace, contracts, _hashes, _envelope = _qualification_store(tmp_path)
    first = _reserve(store, token, contracts["ordinary"])
    reopened = ControlStore(store.db_path)
    replay = _reserve(reopened, token, contracts["ordinary"])
    assert replay.id == first.id and replay.reused
    changed = json.loads(json.dumps(contracts["ordinary"]))
    changed["probe_contract"]["command_sha256"] = "9" * 64
    with pytest.raises(OwnershipRefused, match="INVALID_QUALIFICATION_CONTRACT"):
        _reserve(store, token, changed)
    with store.read_transaction() as tx:
        assert tuple(tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()) == (1, 7)
        retained = tx.execute(
            "SELECT probe_name,contract_json,qualification_envelope_sha256 "
            "FROM authority_qualification_launches"
        ).fetchone()
    assert retained["probe_name"] == "ordinary"
    assert json.loads(retained["contract_json"]) == contracts["ordinary"]


def test_closed_contract_rejects_argv_and_normal_managed_launch_still_needs_receipt(tmp_path):
    store, token, _workspace, contracts, _hashes, _envelope = _qualification_store(tmp_path)
    arbitrary = dict(contracts["ordinary"])
    arbitrary["argv"] = ["/bin/sh", "-c", "arbitrary"]
    with pytest.raises(OwnershipRefused, match="INVALID_QUALIFICATION_CONTRACT"):
        _reserve(store, token, arbitrary)
    with pytest.raises(OwnershipRefused, match="RUNTIME_RECEIPT_REQUIRED"):
        store.reserve_launch(
            "inventory-activity", token, token_reservation=7,
            request_key="ordinary-managed", request_payload={},
            managed_input_sha256=INPUT_SHA,
        )
    with store.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0


@pytest.mark.parametrize("boundary,committed", [
    ("reserve_qualification_launch.after_write_before_commit", False),
    ("reserve_qualification_launch.after_commit_before_return", True),
])
def test_qualification_reservation_fault_boundary_is_atomic(tmp_path, boundary, committed):
    store, token, _workspace, contracts, _hashes, _envelope = _qualification_store(tmp_path)

    def fault(point):
        if point == boundary:
            raise RuntimeError(boundary)

    store.fault_probe = fault
    with pytest.raises(RuntimeError, match=boundary):
        _reserve(store, token, contracts["ordinary"])
    reopened = ControlStore(store.db_path)
    with reopened.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == int(committed)
        assert tuple(tx.execute(
            "SELECT dispatch_used,token_committed FROM authority_run_limits"
        ).fetchone()) == ((1, 7) if committed else (0, 0))
    result = _reserve(reopened, token, contracts["ordinary"])
    assert result.reused is committed


def test_dispatch_limit_cannot_be_reset_to_bypass_qualification_accounting(tmp_path):
    store, token, _workspace, contracts, _hashes, _envelope = _qualification_store(
        tmp_path, dispatch_limit=1,
    )
    first = _reserve(store, token, contracts["ordinary"])
    assert _reserve(store, token, contracts["ordinary"]).id == first.id
    with pytest.raises(OwnershipRefused, match="RUN_LIMITS_IMMUTABLE"):
        store.configure_run_limits(token, dispatch_limit=2, token_limit=100, worker_capacity=2)
    with store.transaction() as tx:
        tx.execute(
            "UPDATE authority_launch_intents SET state='completed_failed',completion_status='failed' "
            "WHERE id=?", (first.id,),
        )
    with pytest.raises(OwnershipRefused, match="DISPATCH_LIMIT_EXHAUSTED"):
        _reserve(store, token, contracts["native-positive"])
    with store.read_transaction() as tx:
        assert tx.execute(
            "SELECT remaining_retry_budget FROM authority_activities "
            "WHERE id='inventory-activity'"
        ).fetchone()[0] == 3


def test_four_completed_probes_promote_activity_binding_and_workspace_atomically(tmp_path):
    store, token, workspace_path, contracts, hashes, envelope = _qualification_store(tmp_path)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    for index, name in enumerate(PROBES):
        intent = _reserve(store, token, contracts[name])
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            acknowledgement = store.acknowledge_child(
                intent.id, token, ProcessIdentity.from_pid(child.pid),
            )
            store.authorize_child(acknowledgement, token)
        finally:
            child.terminate()
            child.wait(timeout=10)
        receipt = _publish(evidence_root, f"probe-{index}.json", {"probe": name})
        store.complete_launch(
            intent.id, token, status="succeeded", evidence=receipt, token_usage=0,
        )
    observation = _publish(evidence_root, "qualification-observation.json", {"qualified": True})
    qualified = _qualified(workspace_path)
    runtime_identity = store.runtime_tuple_hash(qualified)
    promoted = store.promote_qualified_activity(
        token, "inventory-activity", qualification_request_key="qualification-wave",
        expected_contract_hashes=hashes, runtime_identity=runtime_identity,
        final_contract_hash="9" * 64, role="worker", observation_evidence=observation,
    )
    assert promoted.runtime_tuple_hash == runtime_identity
    replay = ControlStore(store.db_path).promote_qualified_activity(
        token, "inventory-activity", qualification_request_key="qualification-wave",
        expected_contract_hashes=dict(reversed(tuple(hashes.items()))),
        runtime_identity=runtime_identity, final_contract_hash="9" * 64,
        role="worker", observation_evidence=observation,
    )
    assert replay.reused_result
    receipt = store.commit_runtime_receipt(token, "inventory-activity", qualified)
    assert receipt.producer_activity_id == "inventory-activity"
    with store.read_transaction() as tx:
        binding = tx.execute(
            "SELECT role,runtime_identity,contract_hash FROM authority_child_bindings "
            "WHERE activity_id='inventory-activity'"
        ).fetchone()
        workspace = tx.execute(
            "SELECT child_role FROM context_workspaces WHERE preparation_id='preparation'"
        ).fetchone()
        assert tx.execute("SELECT COUNT(*) FROM authority_qualification_promotions").fetchone()[0] == 1
    assert tuple(binding) == ("worker", runtime_identity, "9" * 64)
    assert workspace[0] == "worker"
    assert envelope != promoted.runtime_tuple_hash


def test_promotion_refuses_partial_or_arbitrary_handoff(tmp_path):
    store, token, _workspace, contracts, hashes, _envelope = _qualification_store(tmp_path)
    _reserve(store, token, contracts["ordinary"])
    observation = _publish(tmp_path, "partial-observation.json", {"qualified": False})
    with pytest.raises(OwnershipRefused, match="INVALID_QUALIFICATION_PROMOTION"):
        store.promote_qualified_activity(
            token, "inventory-activity", qualification_request_key="qualification-wave",
            expected_contract_hashes=hashes, runtime_identity="8" * 64,
            final_contract_hash="9" * 64, role="inventory", observation_evidence=observation,
        )
    with pytest.raises(OwnershipRefused, match="QUALIFICATION_INCOMPLETE"):
        store.promote_qualified_activity(
            token, "inventory-activity", qualification_request_key="qualification-wave",
            expected_contract_hashes=hashes, runtime_identity="8" * 64,
            final_contract_hash="9" * 64, role="reviewer", observation_evidence=observation,
        )
    with store.read_transaction() as tx:
        assert tx.execute(
            "SELECT role FROM authority_child_bindings WHERE activity_id='inventory-activity'"
        ).fetchone()[0] == "inventory"


def test_caller_selected_child_activity_id_is_exact_and_collision_safe(tmp_path, monkeypatch):
    def execute(primary, store, token, context):
        store.configure_run_limits(token, dispatch_limit=1, token_limit=10)
        store.bind_runtime(token, context.activity_id, "b" * 64)
        pending = begin_child_workspace_preparation(
            store, token, parent_activity_id=context.activity_id,
            request_key="chosen-id-workspace", role="inventory",
            base_commit=git(primary, "rev-parse", "HEAD"), repository_path=primary,
            selected_input_manifest={"entries": []},
        )
        ready = prepare_workspace(store, token, pending)
        chosen = str(uuid.uuid4())
        kwargs = dict(
            parent_activity_id=context.activity_id, role="inventory",
            request_key="chosen-id-activity", candidate_hash=ready.input_digest,
            contract_hash="d" * 64, runtime_identity="e" * 64,
            workspace_binding=str(ready.path), workspace_preparation_id=ready.id,
            activity_id=chosen,
        )
        created = store.create_child_activity(token, **kwargs)
        assert created.id == chosen
        assert store.create_child_activity(token, **kwargs).id == chosen
        with pytest.raises(OwnershipRefused, match="IDEMPOTENCY_CONFLICT"):
            store.create_child_activity(token, **{**kwargs, "activity_id": str(uuid.uuid4())})
        with pytest.raises(OwnershipRefused, match="ACTIVITY_ID_CONFLICT"):
            store.create_child_activity(
                token, **{**kwargs, "request_key": "different-request"},
            )
        with pytest.raises(OwnershipRefused, match="INVALID_CHILD_ACTIVITY_ID"):
            store.create_child_activity(
                token, **{**kwargs, "activity_id": chosen.upper()},
            )
    _invoke(tmp_path, monkeypatch, execute)
