"""Real local sealed checks; no native model or synthetic native receipt."""
import hashlib
import json
from pathlib import Path
import time

import pytest

from run_state.frontend_policy import FrontendPolicyController, FrontendPolicyRefused
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.supervisor import Supervisor
from run_state.ownership import OwnershipRefused
from test_managed_production_ingress import _setup
from test_supervised_process import _allocate_registered_child


@pytest.mark.parametrize('dirty_overlay', [False, True])
def test_sealed_check_uses_actual_isolated_process_and_reuses_only_unchanged_candidate(tmp_path, monkeypatch, dirty_overlay):
    primary, authority, _repository_id, env = _setup(tmp_path)
    if dirty_overlay:
        changed = b'base input with the selected candidate patch\n'
        (primary / 'src/input.txt').write_bytes(changed)
        selected_path = Path(env['FFS_SELECTION_MANIFEST'])
        selected = json.loads(selected_path.read_bytes())
        selected['entries'] = [{'operation': 'copy', 'path': 'src/input.txt',
            'sha256': hashlib.sha256(changed).hexdigest(), 'git_mode': '100644'}]
        selected_path.write_text(json.dumps(selected))
    monkeypatch.chdir(primary)
    seen = []

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, 'b' * 64)
        parent, ready = _allocate_registered_child(store, token, key='check-parent')
        store.transition_activity(token, parent.id, expected='pending', new='active', reason='registered candidate fixture')
        legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
        controller = FrontendPolicyController(store, token, command_mode='feature-implement')
        material = build_frontend_acceptance_draft(
            objective_digest=legacy.material['objective_digest'],
            criteria=[{'id': legacy.accepted_requirement_ids[0], 'objective_clause': 'retain the base input text',
                'checks': [{'id': 'input-text', 'kind': 'command',
                            'locator': "/usr/bin/grep -q 'base input' src/input.txt"}],
                'evidence_rules': [{'id': 'check-process', 'kind': 'test', 'required': True}]}],
            exclusions=[{'id': 'other-source', 'reason': 'no source changes requested'}],
            global_invariants=[{'id': 'no-commit', 'reason': 'retain initial HEAD'}],
            requested_runtime_hash='b' * 64, effective_runtime_hash='b' * 64,
            candidate_hash=ready.input_digest, generation=legacy.generation, command_mode='feature-implement')
        frozen = controller.freeze(draft_id='local-check', revision=1, material=material)
        store.transition_frontend_policy(token, expected_stage='SEALED', new_stage='EXECUTE')
        queue = ManagedAdmissionQueue(tmp_path / 'check-resources', observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 4, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
        supervisor = Supervisor(store, token, evidence_root=authority / 'checks-evidence',
            shared_resource_coordinator=SharedResourceCoordinator(store, token, queue=queue),
            resource_demand_policy=cold_start_demand)
        forged = authority / 'plausible-check.log'
        forged.write_text('caller says passed')
        with pytest.raises(OwnershipRefused, match='FRONTEND_CHECK_EXECUTION_REQUIRED'):
            store.record_frontend_check_results(token, acceptance_hash=frozen.acceptance_hash,
                candidate_hash=frozen.candidate_hash, results=[{'check_id':'input-text','status':'passed',
                    'evidence':[{'locator':str(forged),'sha256':hashlib.sha256(forged.read_bytes()).hexdigest()}]}])
        result = controller.run_mapped_checks(workspace=str(ready.path), supervisor=supervisor,
                                               parent_activity_id=parent.id)
        assert result['input-text']['status'] == 'passed'
        with store.read_transaction() as tx:
            intent = tx.execute('SELECT * FROM authority_launch_intents').fetchone()
            assert intent['child_pid'] is not None and intent['completion_status'] == 'succeeded'
            assert tx.execute('SELECT dispatch_used FROM authority_run_limits').fetchone()[0] == 1
            assert tx.execute('SELECT candidate_hash FROM authority_frontend_policy_checks').fetchone()[0] == frozen.candidate_hash
        repeated = controller.run_mapped_checks(workspace=str(ready.path), supervisor=supervisor,
                                                 parent_activity_id=parent.id)
        assert repeated['input-text']['reused']
        assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == 1
        assert all(row['status'] == 'released' for row in queue.snapshot())
        (ready.path / 'src/input.txt').write_text('changed after the check\n')
        with pytest.raises(FrontendPolicyRefused, match='FRONTEND_CHECK_CANDIDATE_STALE'):
            controller.run_mapped_checks(workspace=str(ready.path), supervisor=supervisor, parent_activity_id=parent.id)
        seen.append(frozen.acceptance_hash)
        return 0

    result = prepare_managed_run(objective=env['FFS_OBJECTIVE'], state_root=authority,
        selection_manifest=env['FFS_SELECTION_MANIFEST'],
        upstream_runtime_manifest=env['FFS_UPSTREAM_RUNTIME_MANIFEST'],
        upstream_runtime_sha256=env['FFS_UPSTREAM_RUNTIME_SHA256'], request_key=env['FFS_REQUEST_KEY'],
        run_id=env['GSD_RUN_ID'], command=('/gsd-plan-phase', '1'), activity='plan', scope='1',
        dispatch_limit=8, token_limit=1000, on_ready=execute)
    assert result == 0 and len(seen) == 1
