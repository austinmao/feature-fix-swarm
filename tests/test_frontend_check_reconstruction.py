"""Interrupted local-check reconstruction: completed physical checks are reused, never relaunched."""
import os
import sys
import time

import pytest

requires_local_confinement = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"),
    reason="needs Darwin sandbox-exec local-check confinement (non-Darwin fails closed; covered by test_local_check_transport)",
)
pytestmark = requires_local_confinement

from run_state.frontend_policy import FrontendPolicyController, FrontendPolicyRefused
from run_state.managed import build_frontend_acceptance_draft, prepare_managed_run
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.supervisor import Supervisor
from test_managed_production_ingress import _setup
from test_supervised_process import _allocate_registered_child


def test_checks_completed_before_publication_are_reconstructed_without_relaunch(tmp_path, monkeypatch):
    primary, authority, _repository_id, env = _setup(tmp_path)
    monkeypatch.chdir(primary)
    seen = []

    def execute(store, token, context):
        store.bind_runtime(token, context.activity_id, 'b' * 64)
        parent, ready = _allocate_registered_child(store, token, key='check-parent')
        store.transition_activity(token, parent.id, expected='pending', new='active', reason='candidate')
        legacy = store.get_acceptance_contract(repository_id=token.repository_id, run_id=token.run_id)
        controller = FrontendPolicyController(store, token, command_mode='feature-implement')
        material = build_frontend_acceptance_draft(
            objective_digest=legacy.material['objective_digest'],
            criteria=[{'id': legacy.accepted_requirement_ids[0], 'objective_clause': 'retain the base input text',
                'checks': [{'id': 'input-text', 'kind': 'command', 'locator': "/usr/bin/grep -q 'base input' src/input.txt"},
                           {'id': 'input-exists', 'kind': 'command', 'locator': '/bin/test -f src/input.txt'}],
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
        first = controller.run_mapped_checks(workspace=str(ready.path), supervisor=supervisor, parent_activity_id=parent.id)
        assert {k: v['status'] for k, v in first.items()} == {'input-text': 'passed', 'input-exists': 'passed'}
        charged = store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged
        assert charged == 2
        # Simulate the owner dying after both physical checks completed but before publication.
        with store.transaction() as tx:
            tx.execute('DELETE FROM authority_frontend_policy_checks')
        again = controller.run_mapped_checks(workspace=str(ready.path), supervisor=supervisor, parent_activity_id=parent.id)
        assert {k: v['status'] for k, v in again.items()} == {'input-text': 'passed', 'input-exists': 'passed'}
        assert {k: v['evidence'] for k, v in again.items()} == {k: v['evidence'] for k, v in first.items()}
        assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == charged
        with store.read_transaction() as tx:
            assert tx.execute('SELECT COUNT(*) FROM authority_launch_intents').fetchone()[0] == 2
            assert tx.execute('SELECT COUNT(*) FROM authority_frontend_policy_checks').fetchone()[0] == 2
        # An existing check child whose intent never settled is reconciled explicitly, never relaunched.
        with store.transaction() as tx:
            tx.execute('DELETE FROM authority_frontend_policy_checks')
            tx.execute("UPDATE authority_launch_intents SET state='reconcile_required',completion_status='uncertain' "
                       "WHERE id=(SELECT id FROM authority_launch_intents LIMIT 1)")
        with pytest.raises(FrontendPolicyRefused, match='FRONTEND_CHECK_RECONCILIATION_REQUIRED'):
            controller.run_mapped_checks(workspace=str(ready.path), supervisor=supervisor, parent_activity_id=parent.id)
        assert store.get_run_policy_budget(repository_id=token.repository_id, run_id=token.run_id).launch_charged == charged
        seen.append(frozen.acceptance_hash)
        return 0

    result = prepare_managed_run(objective=env['FFS_OBJECTIVE'], state_root=authority,
        selection_manifest=env['FFS_SELECTION_MANIFEST'],
        upstream_runtime_manifest=env['FFS_UPSTREAM_RUNTIME_MANIFEST'],
        upstream_runtime_sha256=env['FFS_UPSTREAM_RUNTIME_SHA256'], request_key=env['FFS_REQUEST_KEY'],
        run_id=env['GSD_RUN_ID'], command=('/gsd-plan-phase', '1'), activity='plan', scope='1',
        dispatch_limit=8, token_limit=1000, on_ready=execute)
    assert result == 0 and len(seen) == 1
