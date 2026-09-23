"""Positive winner-integration proof over the existing journal/authority.

Python trial children and fixture runtime receipts are explicit here. This
proves the recovery journal key family, the recovery candidate chain, the
candidate binding and the saved-stage continuation, not native model
diagnosis or a native trial producer.
"""
import json
import os
from pathlib import Path
import sys
import time

import pytest

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)
pytestmark = requires_local_confinement

from run_state.candidate_chain import verify_candidate_chain
from run_state.frontend_policy import FrontendPolicyController
from run_state.integration_journal import read_intent
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.ownership import OwnershipRefused
from run_state.recovery_controller import (
    ControlStoreRecoveryAuthority, FrozenRecoveryBinding, RecoveryController, RecoveryDecision, RecoveryRefused,
    RecoveryTrial,
)
from run_state.recovery_integration import integrate_recovery_winner, recovery_journal_key
from run_state.recovery_trial_checks import run_isolated_trial_checks
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import Supervisor
from run_state.wave_execution import _head
from run_state.workspace import load_input_snapshot
from test_managed_production_ingress import _setup
from test_recovery_controller import docs
from test_recovery_trial_checks import _launch, _recovery_child
from test_runtime_receipt_authority import _qualified
from test_supervised_process import _allocate_registered_child


def test_recovery_winner_is_journaled_bound_and_continues_saved_stage(tmp_path, monkeypatch):
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    seen = []

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, 'b' * 64)
        parent, ready = _allocate_registered_child(store, token, key='shared-candidate')
        store.transition_activity(token, parent.id, expected='pending', new='active', reason='shared candidate')
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
        frozen = controller.freeze(draft_id='winner-seal', revision=1, material=material)
        store.transition_frontend_policy(token, expected_stage='SEALED', new_stage='EXECUTE')
        packet = controller.handback(saved_stage='EXECUTE', failed_criteria=[criterion], consumed_attempts=[], choices=[])
        snapshot = load_input_snapshot(store, ready)
        binding = FrozenRecoveryBinding(ready.base_commit, ready.input_digest, frozen.candidate_hash,
                                        material['candidate_hash'], frozen.acceptance_hash, runtime, frozen.acceptance_hash)
        cycle = store.reserve_policy_action(token, action='recovery_cycle_normal', logical_key='cycle-1',
                                            input_hash=binding.input_hash, recovery_cycle=1)
        queue = ManagedAdmissionQueue(tmp_path / 'winner-resources', observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
        supervisor = Supervisor(store, token, evidence_root=authority / 'winner-evidence',
            shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
            resource_demand_policy=cold_start_demand)

        diagnosis, diagnosis_ready = _recovery_child(store, token, parent=parent, ready=ready, snapshot=snapshot,
            key='diagnosis', acceptance_hash=frozen.acceptance_hash, runtime=runtime)
        _a, diagnosis_receipt, _r, _rt, diagnosis_evidence = _launch(store, token, supervisor, child=diagnosis,
            ready=diagnosis_ready, script='pass', key='diagnosis-launch', acceptance_hash=frozen.acceptance_hash,
            action='diagnosis', cycle=1)
        store.transition_activity(token, diagnosis.id, expected='active', new='succeeded', result=diagnosis_evidence)
        trials = []
        for name, suffix in (('trial-a', 'repaired by the longer trial patch\n'), ('trial-b', 'repaired\n')):
            child, child_ready = _recovery_child(store, token, parent=parent, ready=ready, snapshot=snapshot,
                key=name, acceptance_hash=frozen.acceptance_hash, runtime=runtime)
            script = ("from pathlib import Path\np=Path('src/input.txt')\n"
                      f"p.write_text(p.read_text()+{suffix!r})\n")
            action_id, receipt, _r, _rt, evidence = _launch(store, token, supervisor, child=child, ready=child_ready,
                script=script, key=name + '-launch', acceptance_hash=frozen.acceptance_hash,
                action='recovery_trial', cycle=1)
            run_isolated_trial_checks(store, token, supervisor=supervisor, cycle_action_id=cycle.id,
                trial_action_id=action_id, trial_activity_id=child.id, workspace=str(child_ready.path),
                expected_input_digest=ready.input_digest)
            store.transition_activity(token, child.id, expected='active', new='succeeded', result=evidence)
            trials.append(RecoveryTrial(action_id, receipt))
        recovery = RecoveryController(packet, binding, docs(), ControlStoreRecoveryAuthority(store, token, binding))
        decision = recovery.consume(cycle.id, diagnosis_receipt=diagnosis_receipt, trials=trials)
        assert decision.winner['action_id'] == trials[1].action_id
        before_text = (ready.path / 'src/input.txt').read_text()

        # A decision without a verified winner integrates nothing.
        with pytest.raises(RecoveryRefused, match='RECOVERY_WINNER_REQUIRED'):
            integrate_recovery_winner(store, token, supervisor=supervisor, workspace=str(ready.path),
                decision=RecoveryDecision(decision.saved_stage, decision.cycle, decision.diagnosis_receipt_hash,
                                          decision.documents, 'NEEDS_DECISION'))
        launches = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged
        # Interrupted after git apply, before any record: the pending journal reconstructs, never re-applies.
        import run_state.wave_execution as wave_execution
        with monkeypatch.context() as patched:
            patched.setattr(wave_execution, 'capture_integration_candidate',
                            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('interrupted')))
            with pytest.raises(RuntimeError, match='interrupted'):
                integrate_recovery_winner(store, token, supervisor=supervisor, decision=decision,
                                          workspace=str(ready.path))
        assert (ready.path / 'src/input.txt').read_text() == before_text + 'repaired\n'
        assert read_intent(store, token, wave_key=recovery_journal_key(decision.winner['action_id']))['state'] == 'pending'
        assert store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id).stage == 'RECOVER'
        continued = integrate_recovery_winner(store, token, supervisor=supervisor, decision=decision,
                                              workspace=str(ready.path))
        # Exact retained winner bytes landed in the shared candidate; HEAD never moved; no launch was charged.
        assert (ready.path / 'src/input.txt').read_text() == before_text + 'repaired\n'
        assert _head(ready.path) == ready.base_commit
        assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == launches
        state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
        assert (state.stage, state.candidate_hash) == ('EXECUTE', continued['candidate_hash'])
        assert continued['candidate_hash'] != frozen.candidate_hash
        assert continued['parent_candidate_hash'] == frozen.candidate_hash
        assert continued['remaining_obligation_ids'] == [criterion] and continued['consumed_attempts'] == []
        assert state.decision_json == continued
        journal = read_intent(store, token, wave_key=recovery_journal_key(decision.winner['action_id']))
        assert journal['state'] == 'published' and journal['activity_id'] == decision.winner['activity_id']
        with store.read_transaction() as tx:
            bound = tx.execute('SELECT receipt_hash,parent_candidate_hash FROM authority_frontend_policy_candidates '
                               'WHERE candidate_hash=?', (continued['candidate_hash'],)).fetchone()
            events = tx.execute('SELECT COUNT(*) FROM control_events').fetchone()[0]
            integration = json.loads(tx.execute(
                'SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id '
                'WHERE k.idempotency_key=?', ('frontend-integration:' + decision.winner['receipt_hash'],),
            ).fetchone()['payload'])['data']
        assert tuple(bound) == (decision.winner['receipt_hash'], frozen.candidate_hash)
        assert integration['no_commit_evidence'] is None
        # Replay returns the retained continuation without another event or effect.
        assert integrate_recovery_winner(store, token, supervisor=supervisor, decision=decision,
                                         workspace=str(ready.path)) == continued
        with store.read_transaction() as tx:
            assert tx.execute('SELECT COUNT(*) FROM control_events').fetchone()[0] == events
        # The saved stage really continues: frozen checks now pass on the advanced shared candidate.
        from run_state.candidate_chain import resolve_current_frontend_candidate
        current = resolve_current_frontend_candidate(store, token)
        assert current is not None
        # The winning trial is terminal and lives in a sibling workspace.  The
        # resolver must choose its active legacy-contract parent instead.
        assert (current.candidate_hash, current.workspace, current.parent_activity_id,
                current.workspace_preparation_id) == (
            continued['candidate_hash'], str(ready.path), parent.id, ready.id)
        with store.read_transaction() as tx:
            binding = tx.execute('SELECT contract_hash FROM authority_child_bindings WHERE activity_id=?',
                                 (parent.id,)).fetchone()
        assert binding['contract_hash'] != frozen.acceptance_hash
        checks = controller.run_mapped_checks(workspace=current.workspace, supervisor=supervisor,
                                              parent_activity_id=current.parent_activity_id)
        assert checks['repaired']['status'] == 'passed'
        # The chain re-verifies physical bytes: a changed journaled record or a moved candidate is refused.
        proof = dict(receipt_hash=decision.winner['receipt_hash'], candidate_hash=continued['candidate_hash'],
                     integration_evidence=integration['integration_evidence'], no_commit_evidence=None)
        assert verify_candidate_chain(store, token, **proof) == integration['candidate_chain']
        with pytest.raises(OwnershipRefused, match='FRONTEND_INTEGRATION_CANDIDATE_INVALID'):
            verify_candidate_chain(store, token, **{**proof, 'no_commit_evidence': integration['integration_evidence']})
        (ready.path / 'src/input.txt').write_text(before_text + 'repaired, then changed\n')
        with pytest.raises(OwnershipRefused, match='FRONTEND_INTEGRATION_CANDIDATE_STALE'):
            verify_candidate_chain(store, token, **proof)
        seen.append(continued['candidate_hash'])
        return 0

    result = prepare_managed_run(objective=env['FFS_OBJECTIVE'], state_root=authority,
        selection_manifest=env['FFS_SELECTION_MANIFEST'],
        upstream_runtime_manifest=env['FFS_UPSTREAM_RUNTIME_MANIFEST'],
        upstream_runtime_sha256=env['FFS_UPSTREAM_RUNTIME_SHA256'], request_key=env['FFS_REQUEST_KEY'],
        run_id=env['GSD_RUN_ID'], command=('/gsd-plan-phase', '1'), activity='plan', scope='1',
        dispatch_limit=12, token_limit=1000, on_ready=execute)
    assert result == 0 and len(seen) == 1
