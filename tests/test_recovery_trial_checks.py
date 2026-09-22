"""Positive isolated-trial proof: real diagnosis/trial children, real confined checks.

Python child processes and fixture runtime receipts are explicit here. This
proves the trial-specific check authority and winner selection, not native
model diagnosis, winner integration or saved-obligation continuation.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import pytest

from run_state.frontend_policy import FrontendPolicyController
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.recovery_controller import (
    ControlStoreRecoveryAuthority, FrozenRecoveryBinding, RecoveryController, RecoveryRefused, RecoveryTrial,
)
from run_state.recovery_trial_checks import run_isolated_trial_checks
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.supervisor import DispatchRequest, Supervisor
from run_state.workspace import begin_child_workspace_preparation, inspect_workspace, load_input_snapshot, prepare_workspace
from test_managed_production_ingress import _setup
from test_recovery_controller import docs
from test_runtime_receipt_authority import _qualified
from test_supervised_process import _allocate_registered_child, _receipt_bound_request
from run_state.state import qualified_runtime_tuple_hash


def _recovery_child(store, token, *, parent, ready, snapshot, key, acceptance_hash, runtime):
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=parent.id, request_key=key + ':workspace', role='recovery',
        base_commit=ready.base_commit, selected_input_manifest=snapshot.manifest,
        repository_path=ready.repository_path)
    child_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
    child = store.create_child_activity(
        token, parent_activity_id=parent.id, role='recovery', request_key=key + ':activity',
        candidate_hash=child_ready.input_digest, contract_hash=acceptance_hash, runtime_identity=runtime,
        workspace_binding=str(child_ready.path), workspace_preparation_id=child_ready.id, retry_budget=1)
    return child, child_ready


def _launch(store, token, supervisor, *, child, ready, script, key, acceptance_hash, action, cycle):
    store.transition_activity(token, child.id, expected='pending', new='active', reason='fixture recovery child')
    # Each isolated recovery child binds its own workspace-bound runtime receipt.
    request = _receipt_bound_request(store, token, DispatchRequest(
        child.id, key, (sys.executable, '-c', script), str(ready.path), ready.base_commit, 'b' * 64,
        contract_hash=acceptance_hash))
    request = supervisor.reserve_request_action(request, action=action, recovery_cycle=cycle)
    handle = supervisor.launch(request)
    result = supervisor.finish(handle, timeout=30, token_usage=0)
    assert result['returncode'] == 0, result
    receipt = {
        'schema': 'ffs.run-policy-receipt/v1', 'role': 'recovery', 'request_key': key,
        'activity_id': child.id, 'intent_id': handle.intent_id, 'fence_generation': token.generation,
        'acceptance_hash': acceptance_hash, 'candidate_hash': ready.input_digest,
        'runtime_hash': request.runtime_identity,
        'workspace_preparation_hash': hashlib.sha256(json.dumps(
            asdict(inspect_workspace(store, ready.id)), default=str, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'evidence': [{'id': 'process-result', **result['evidence']}],
        'completion_status': 'succeeded', 'process_identity': asdict(handle.identity), 'review_dimensions': [],
    }
    recorded = store.record_acceptance_receipt(token, acceptance_hash=acceptance_hash, receipt=receipt)
    return request.policy_action_id, receipt, recorded, request.runtime_identity, result['evidence']


def test_isolated_trial_checks_bind_patch_and_candidate_without_advancing_shared_state(tmp_path, monkeypatch):
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    seen = []

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, 'b' * 64)
        parent, ready = _allocate_registered_child(store, token, key='shared-candidate')
        store.transition_activity(token, parent.id, expected='pending', new='active', reason='shared candidate')
        # Sealed effective runtime: the shared candidate's qualified runtime.
        runtime = qualified_runtime_tuple_hash(_qualified(Path(ready.path)))
        legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
        criterion = legacy.accepted_requirement_ids[0]
        controller = FrontendPolicyController(store, token, command_mode='feature-implement')
        material = build_frontend_acceptance_draft(
            objective_digest=legacy.material['objective_digest'],
            criteria=[{'id': criterion, 'objective_clause': 'the input records the repair',
                'checks': [{'id': 'repaired', 'kind': 'command', 'locator': '/usr/bin/grep -q repaired src/input.txt'}],
                'evidence_rules': [{'id': 'check-process', 'kind': 'test', 'required': True}]}],
            exclusions=[{'id': 'other-source', 'reason': 'only the input may change'}],
            global_invariants=[{'id': 'no-commit', 'reason': 'retain initial HEAD'}],
            requested_runtime_hash=runtime, effective_runtime_hash=runtime,
            candidate_hash=ready.input_digest, generation=legacy.generation, command_mode='feature-implement')
        frozen = controller.freeze(draft_id='trial-seal', revision=1, material=material)
        store.transition_frontend_policy(token, expected_stage='SEALED', new_stage='EXECUTE')
        packet = controller.handback(saved_stage='EXECUTE', failed_criteria=[criterion], consumed_attempts=[], choices=[])
        assert store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id).stage == 'RECOVER'

        snapshot = load_input_snapshot(store, ready)
        binding = FrozenRecoveryBinding(ready.base_commit, ready.input_digest, frozen.candidate_hash,
                                        material['candidate_hash'], frozen.acceptance_hash, runtime, frozen.acceptance_hash)
        cycle = store.reserve_policy_action(token, action='recovery_cycle_normal', logical_key='cycle-1',
                                            input_hash=binding.input_hash, recovery_cycle=1)
        queue = ManagedAdmissionQueue(tmp_path / 'trial-resources', observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
        supervisor = Supervisor(store, token, evidence_root=authority / 'trial-evidence',
            shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
            resource_demand_policy=cold_start_demand)

        diagnosis, diagnosis_ready = _recovery_child(store, token, parent=parent, ready=ready, snapshot=snapshot,
            key='diagnosis', acceptance_hash=frozen.acceptance_hash, runtime=runtime)
        _action, diagnosis_receipt, _recorded, _rt, diagnosis_evidence = _launch(store, token, supervisor, child=diagnosis,
            ready=diagnosis_ready, script='pass', key='diagnosis-launch', acceptance_hash=frozen.acceptance_hash,
            action='diagnosis', cycle=1)
        store.transition_activity(token, diagnosis.id, expected='active', new='succeeded', result=diagnosis_evidence)

        trials = []
        for name, suffix in (('trial-a', 'repaired by the longer trial patch\n'), ('trial-b', 'repaired\n')):
            child, child_ready = _recovery_child(store, token, parent=parent, ready=ready, snapshot=snapshot,
                key=name, acceptance_hash=frozen.acceptance_hash, runtime=runtime)
            script = ("from pathlib import Path\np=Path('src/input.txt')\n"
                      f"p.write_text(p.read_text()+{suffix!r})\n")
            action_id, receipt, _recorded, _rt, trial_evidence = _launch(store, token, supervisor, child=child, ready=child_ready,
                script=script, key=name + '-launch', acceptance_hash=frozen.acceptance_hash,
                action='recovery_trial', cycle=1)
            trials.append((name, action_id, receipt, child, child_ready, trial_evidence))

        reader = ControlStoreRecoveryAuthority(store, token, binding)
        recovery = RecoveryController(packet, binding, docs(), reader)
        # No trial has bound check authority yet: the reader refuses rather than trusting the receipts.
        with pytest.raises(RecoveryRefused, match='RECOVERY_TRIAL_CHECKS_REQUIRED'):
            recovery.consume(cycle.id, diagnosis_receipt=diagnosis_receipt,
                             trials=[RecoveryTrial(action_id, receipt) for _n, action_id, receipt, _c, _r, _e in trials])
        launches_before = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged
        records = {}
        for name, action_id, _receipt, child, child_ready, _evidence in trials:
            records[name] = run_isolated_trial_checks(store, token, supervisor=supervisor, cycle_action_id=cycle.id,
                trial_action_id=action_id, trial_activity_id=child.id, workspace=str(child_ready.path),
                expected_input_digest=ready.input_digest)
            assert records[name]['results'][0]['status'] == 'passed'
            assert records[name]['trial_candidate_hash'] != frozen.candidate_hash
            assert Path(records[name]['patch']['locator']).read_bytes().startswith(b'diff --git')
        budget = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id)
        assert budget.launch_charged == launches_before + 2
        # Replay returns the retained record without a new launch or check child.
        # Trial children settle only after their evaluation; replay then needs no active parent.
        for _name, _action, _receipt, child, _ready, evidence in trials:
            store.transition_activity(token, child.id, expected='active', new='succeeded', result=evidence)
        name, action_id, _receipt, child, child_ready, _evidence = trials[0]
        assert run_isolated_trial_checks(store, token, supervisor=supervisor, cycle_action_id=cycle.id,
            trial_action_id=action_id, trial_activity_id=child.id, workspace=str(child_ready.path),
            expected_input_digest=ready.input_digest) == records[name]
        assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == budget.launch_charged
        # Shared candidate/state is untouched: no mapped check rows, same candidate, still RECOVER.
        state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
        assert (state.stage, state.candidate_hash) == ('RECOVER', frozen.candidate_hash)
        with store.read_transaction() as tx:
            assert tx.execute('SELECT COUNT(*) FROM authority_frontend_policy_checks').fetchone()[0] == 0

        decision = recovery.consume(cycle.id, diagnosis_receipt=diagnosis_receipt,
                                    trials=[RecoveryTrial(action_id, receipt) for _n, action_id, receipt, _c, _r, _e in trials])
        assert decision.continuation == 'EXECUTE' and decision.winner is not None
        assert decision.winner['action_id'] == trials[1][1]  # the smallest verified patch wins deterministically
        assert decision.winner['patch_sha256'] == records['trial-b']['patch']['sha256']
        assert {item['action_id'] for item in decision.trials} == {trials[0][1], trials[1][1]}
        assert recovery.consume(cycle.id, diagnosis_receipt=diagnosis_receipt,
                                trials=[RecoveryTrial(action_id, receipt) for _n, action_id, receipt, _c, _r, _e in trials]).winner == decision.winner
        assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == budget.launch_charged
        # Tampered physical check evidence of the winner is refused; the other trial alone still wins.
        evidence_path = Path(records['trial-b']['results'][0]['evidence'][0]['locator'])
        original = evidence_path.read_bytes()
        evidence_path.write_bytes(original + b'\n')
        with pytest.raises(RecoveryRefused, match='RECOVERY_TRIAL_EVIDENCE_INVALID'):
            recovery.consume(cycle.id, diagnosis_receipt=diagnosis_receipt,
                             trials=[RecoveryTrial(trials[1][1], trials[1][2])])
        evidence_path.write_bytes(original)
        only_a = recovery.consume(cycle.id, diagnosis_receipt=diagnosis_receipt,
                                  trials=[RecoveryTrial(trials[0][1], trials[0][2])])
        assert only_a.winner['action_id'] == trials[0][1]
        # The producer binds the registered trial workspace; another path cannot claim its record.
        with pytest.raises(RecoveryRefused, match='RECOVERY_TRIAL_WORKSPACE_INVALID'):
            run_isolated_trial_checks(store, token, supervisor=supervisor, cycle_action_id=cycle.id,
                trial_action_id=trials[0][1], trial_activity_id=trials[0][3].id, workspace=str(ready.path),
                expected_input_digest=ready.input_digest)
        seen.append(decision.winner['trial_candidate_hash'])
        return 0

    result = prepare_managed_run(objective=env['FFS_OBJECTIVE'], state_root=authority,
        selection_manifest=env['FFS_SELECTION_MANIFEST'],
        upstream_runtime_manifest=env['FFS_UPSTREAM_RUNTIME_MANIFEST'],
        upstream_runtime_sha256=env['FFS_UPSTREAM_RUNTIME_SHA256'], request_key=env['FFS_REQUEST_KEY'],
        run_id=env['GSD_RUN_ID'], command=('/gsd-plan-phase', '1'), activity='plan', scope='1',
        dispatch_limit=12, token_limit=1000, on_ready=execute)
    assert result == 0 and len(seen) == 1
