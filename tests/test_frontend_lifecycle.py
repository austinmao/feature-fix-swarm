"""Frontend lifecycle consumer: SEALED -> EXECUTE -> RECOVER -> EXECUTE -> FINAL_REVIEW -> DONE.

Python children and fixture runtime receipts are explicit here. This proves the
stage-resumable driver sequencing the existing authority components, not a
native host execution, native diagnosis/trials or an opposite-vendor review.
"""
from pathlib import Path
import sys
import time

import pytest

from run_state.frontend_completion import post_repair_review_tx
from run_state.frontend_lifecycle import LifecycleProducers, drive_frontend_lifecycle
from run_state.frontend_policy import FrontendPolicyController, FrontendPolicyRefused
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.recovery_controller import (
    ControlStoreRecoveryAuthority, FrozenRecoveryBinding, RecoveryController, RecoveryTrial,
)
from run_state.recovery_trial_checks import run_isolated_trial_checks
from run_state.resource_observation import ResourceObservation
from run_state.run_policy import action_limit
from run_state.sealed_review import record_final_review
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import qualified_runtime_tuple_hash
from run_state.supervisor import DispatchRequest, Supervisor
from run_state.wave_execution import _head, capture_prelaunch_snapshot
from run_state.workspace import begin_child_workspace_preparation, load_input_snapshot, prepare_workspace
from test_managed_production_ingress import _setup
from test_recovery_controller import docs
from test_recovery_trial_checks import _launch, _recovery_child
from test_runtime_receipt_authority import _qualified
from test_supervised_process import _allocate_registered_child, _receipt_bound_request

_REVIEW = """from pathlib import Path
import hashlib,json,sys
path=Path('src/input.txt').resolve()
print(json.dumps({'schema':'ffs.sealed-final-review/v1','acceptance_hash':sys.argv[1],
 'candidate_hash':sys.argv[2],'review_dimensions':['correctness','security','regression'],
 'criteria':{sys.argv[3]:{'status':sys.argv[4],'evidence':[{'id':'check-process','locator':str(path),
 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}},'findings':[]}))
"""


@pytest.mark.parametrize('scenario', ['repaired', 'unrelated', 'review-fails', 'repair-exhausted',
                                      'repair-uncharged', 'post-repair'])
def test_driver_recovers_a_failed_check_and_reaches_done_resumably(tmp_path, monkeypatch, scenario):
    trial_text = 'unrelated' if scenario == 'unrelated' else 'repaired'
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
                'checks': [{'id': 'repaired', 'kind': 'command', 'locator': '/usr/bin/grep %s src/input.txt' % (
                    '-qv absent-marker' if scenario == 'post-repair' else '-q repaired')}],
                'evidence_rules': [{'id': 'check-process', 'kind': 'test', 'required': True}]}],
            exclusions=[{'id': 'other-source', 'reason': 'only the input may change'}],
            global_invariants=[{'id': 'no-commit', 'reason': 'retain initial HEAD'}],
            requested_runtime_hash=runtime, effective_runtime_hash=runtime,
            candidate_hash=ready.input_digest, generation=legacy.generation, command_mode='feature-implement')
        frozen = controller.freeze(draft_id='lifecycle-seal', revision=1, material=material)
        queue = ManagedAdmissionQueue(tmp_path / 'lifecycle-resources', observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
        supervisor = Supervisor(store, token, evidence_root=authority / 'lifecycle-evidence',
            shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
            resource_demand_policy=cold_start_demand)
        calls = {'execute': 0, 'recover': 0, 'review': 0}
        retained = {}

        def run_execute(_frozen):
            calls['execute'] += 1  # fixture execution leaves the criterion unmet

        def recover(packet):
            calls['recover'] += 1
            if calls['recover'] == 1:
                raise RuntimeError('interrupted before the recovery cycle')
            assert packet['saved_stage'] == ('FINAL_REVIEW' if scenario == 'post-repair' else 'EXECUTE')
            assert packet['failed_criteria'] == [criterion]
            tier = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).tier
            spent = repairs + (['final_review'] if scenario == 'post-repair' else [])
            # Same-timestamp reservations tie-break by id; order carries no meaning.
            assert sorted(packet['consumed_attempts']) == sorted(spent) and len(repairs) == (
                action_limit('repair', tier) if scenario == 'repair-exhausted' else 0)
            snapshot = load_input_snapshot(store, ready)
            binding = FrozenRecoveryBinding(ready.base_commit, ready.input_digest, frozen.candidate_hash,
                                            material['candidate_hash'], frozen.acceptance_hash, runtime,
                                            frozen.acceptance_hash)
            cycle = store.reserve_policy_action(token, action='recovery_cycle_normal', logical_key='cycle-1',
                                                input_hash=binding.input_hash, recovery_cycle=1)
            diagnosis, diagnosis_ready = _recovery_child(store, token, parent=parent, ready=ready, snapshot=snapshot,
                key='diagnosis', acceptance_hash=frozen.acceptance_hash, runtime=runtime)
            _a, diagnosis_receipt, _r, _rt, evidence = _launch(store, token, supervisor, child=diagnosis,
                ready=diagnosis_ready, script='pass', key='diagnosis-launch',
                acceptance_hash=frozen.acceptance_hash, action='diagnosis', cycle=1)
            store.transition_activity(token, diagnosis.id, expected='active', new='succeeded', result=evidence)
            child, child_ready = _recovery_child(store, token, parent=parent, ready=ready, snapshot=snapshot,
                key='trial', acceptance_hash=frozen.acceptance_hash, runtime=runtime)
            script = "from pathlib import Path\np=Path('src/input.txt')\np.write_text(p.read_text()+'" + trial_text + "\\n')\n"
            action_id, receipt, _r, _rt, evidence = _launch(store, token, supervisor, child=child, ready=child_ready,
                script=script, key='trial-launch', acceptance_hash=frozen.acceptance_hash,
                action='recovery_trial', cycle=1)
            run_isolated_trial_checks(store, token, supervisor=supervisor, cycle_action_id=cycle.id,
                trial_action_id=action_id, trial_activity_id=child.id, workspace=str(child_ready.path),
                expected_input_digest=ready.input_digest)
            store.transition_activity(token, child.id, expected='active', new='succeeded', result=evidence)
            return RecoveryController(packet, binding, docs(), ControlStoreRecoveryAuthority(store, token, binding)
                                      ).consume(cycle.id, diagnosis_receipt=diagnosis_receipt,
                                                trials=[RecoveryTrial(action_id, receipt)])

        def final_review(current):
            calls['review'] += 1
            with store.read_transaction() as tx:
                bound = tx.execute('SELECT runtime_tuple_hash FROM authority_activities WHERE id=?',
                                   (parent.id,)).fetchone()[0]
            snapshot = capture_prelaunch_snapshot(store, token, ready, activity_id=parent.id,
                                                  runtime_identity=bound, evidence_root=supervisor.evidence_root)
            pending = begin_child_workspace_preparation(
                store, token, parent_activity_id=parent.id, request_key='final-review', role='reviewer',
                base_commit=ready.base_commit, selected_input_manifest=snapshot.manifest,
                repository_path=ready.repository_path)
            review_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
            child = store.create_child_activity(
                token, parent_activity_id=parent.id, role='reviewer', request_key='final-review-activity',
                candidate_hash=review_ready.input_digest, contract_hash=current.acceptance_hash,
                runtime_identity=runtime, workspace_binding=str(review_ready.path),
                workspace_preparation_id=review_ready.id, retry_budget=1)
            store.transition_activity(token, child.id, expected='pending', new='active', reason='fixture review')
            request = _receipt_bound_request(store, token, DispatchRequest(
                child.id, 'final-review-launch',
                (sys.executable, '-c', _REVIEW, current.acceptance_hash, review_ready.input_digest, criterion,
                 'failed' if scenario in {'review-fails', 'post-repair'} else 'passed'),
                str(review_ready.path), review_ready.base_commit, 'b' * 64, contract_hash=current.acceptance_hash))
            request = controller.reserve_stage(supervisor, request, action='final_review')
            handle = supervisor.launch(request)
            supervisor.finish(handle, timeout=30, token_usage=0)
            retained['review'] = handle.result['evidence']
            try:
                record_final_review(supervisor, handle, acceptance_hash=current.acceptance_hash)
            finally:
                store.transition_activity(token, child.id, expected='active', new='succeeded',
                                          result=handle.result['evidence'])

        def settle():
            if store.get_activity(parent.id).state == 'active':
                store.transition_activity(token, parent.id, expected='active', new='succeeded',
                                          result=retained['review'], reason='shared candidate accepted')

        repairs = []

        def repair(_frozen, failed):
            # Fixture repair fixes nothing; each issued attempt still consumes one non-resettable grant.
            assert failed == [criterion]
            if scenario == 'repair-exhausted':
                repairs.append(store.reserve_policy_action(token, action='repair', input_hash='e' * 64,
                                                           logical_key='repair-%d' % len(repairs)).id)

        producers = LifecycleProducers(execute=run_execute, final_review=final_review, recover=recover,
                                       settle=settle, repair=repair if scenario.startswith('repair-') else None)
        drive = lambda: drive_frontend_lifecycle(  # noqa: E731
            store, token, supervisor=supervisor, controller=controller, workspace=str(ready.path),
            parent_activity_id=parent.id, producers=producers)
        if scenario == 'repair-uncharged':
            with pytest.raises(FrontendPolicyRefused, match='FRONTEND_REPAIR_UNCHARGED'):
                drive()
            seen.append(None)
            return 0
        # Interrupted inside RECOVER: the retained stage and handback resume, execution is not repeated.
        with pytest.raises(RuntimeError, match='interrupted'):
            drive()
        state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
        # post-repair: the mapped check passes at once, the broad review refuses, then one recovery and the
        # deterministic gate reach DONE on the descendant candidate without a second review.
        assert state.stage == 'RECOVER' and calls == {'execute': 1, 'recover': 1,
                                                      'review': 1 if scenario == 'post-repair' else 0}
        if scenario == 'post-repair':
            # The refused review sits on the current candidate: it is no ancestor, so it cannot satisfy DONE yet.
            with store.read_transaction() as tx:
                assert post_repair_review_tx(store, tx, token, state.acceptance_hash, state.candidate_hash) is None
        if trial_text == 'unrelated':
            # No trial passed the frozen checks: fail closed, shared candidate untouched, no review spent.
            assert drive() == 'NEEDS_DECISION' and calls == {'execute': 1, 'recover': 2, 'review': 0}
            state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
            assert state.stage == 'NEEDS_DECISION' and state.candidate_hash == frozen.candidate_hash
            assert state.decision_json['code'] == 'RECOVERY_CYCLE_WITHOUT_WINNER'
            assert 'unrelated' not in (ready.path / 'src/input.txt').read_text()
            assert drive() == 'NEEDS_DECISION' and calls['recover'] == 2
            seen.append(state.candidate_hash)
            return 0
        if scenario == 'review-fails':
            # A refused final review hands back every sealed criterion; the normal tier's one recovery
            # cycle is spent, so the run fails closed without a second review or DONE.
            assert drive() == 'NEEDS_DECISION' and calls == {'execute': 1, 'recover': 2, 'review': 1}
            state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
            assert state.decision_json['saved_stage'] == 'FINAL_REVIEW'
            assert state.decision_json['failed_criteria'] == [criterion]
            assert drive() == 'NEEDS_DECISION' and calls['review'] == 1
            seen.append(state.candidate_hash)
            return 0
        assert drive() == 'DONE'
        assert calls == {'execute': 1, 'recover': 2, 'review': 1}
        state = store.get_frontend_policy_state(repository_id=token.repository_id, run_id=token.run_id)
        assert state.stage == 'DONE' and state.candidate_hash != frozen.candidate_hash
        with store.read_transaction() as tx:
            proof = post_repair_review_tx(store, tx, token, state.acceptance_hash, state.candidate_hash)
        assert (proof is not None and proof[1].completion_status == 'failed') == (scenario == 'post-repair')
        assert (ready.path / 'src/input.txt').read_text().endswith('repaired\n')
        assert _head(ready.path) == ready.base_commit
        with store.read_transaction() as tx:
            assert tx.execute('SELECT state FROM authority_activities WHERE id=?',
                              (context.activity_id,)).fetchone()[0] == 'succeeded'
            events = tx.execute('SELECT COUNT(*) FROM control_events').fetchone()[0]
        launches = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged
        # Terminal replay: no producer call, no event, no charge.
        assert drive() == 'DONE' and calls == {'execute': 1, 'recover': 2, 'review': 1}
        with store.read_transaction() as tx:
            assert tx.execute('SELECT COUNT(*) FROM control_events').fetchone()[0] == events
        assert store.get_run_policy_budget(repository_id=token.repository_id,
                                           run_id=token.run_id).launch_charged == launches
        seen.append(state.candidate_hash)
        return 0

    result = prepare_managed_run(objective=env['FFS_OBJECTIVE'], state_root=authority,
        selection_manifest=env['FFS_SELECTION_MANIFEST'],
        upstream_runtime_manifest=env['FFS_UPSTREAM_RUNTIME_MANIFEST'],
        upstream_runtime_sha256=env['FFS_UPSTREAM_RUNTIME_SHA256'], request_key=env['FFS_REQUEST_KEY'],
        run_id=env['GSD_RUN_ID'], command=('/gsd-plan-phase', '1'), activity='plan', scope='1',
        dispatch_limit=16, token_limit=1000, on_ready=execute)
    assert result == 0 and len(seen) == 1
