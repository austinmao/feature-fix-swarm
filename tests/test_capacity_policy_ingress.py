"""Real managed ingress revises a ceiling without changing run identity/budgets."""
import json
import subprocess
import sys

import pytest

from run_state.state import ControlStore


@pytest.mark.parametrize('entry', ['managed-start', 'frontend-start'])
def test_real_cli_capacity_revision_preserves_immutable_request_and_allowances(tmp_path, entry):
    from test_managed_production_ingress import _setup
    if entry == 'frontend-start':
        from test_frontend_production_ingress import _setup
    primary, authority, repository_id, env = _setup(tmp_path)
    argv = [sys.executable, '-m', 'run_state.cli', entry,
        '--objective', env['FFS_OBJECTIVE'], '--state-root', str(authority),
        '--upstream-runtime-manifest', env['FFS_UPSTREAM_RUNTIME_MANIFEST'],
        '--upstream-runtime-sha256', env['FFS_UPSTREAM_RUNTIME_SHA256'],
        '--request-key', env['FFS_REQUEST_KEY'], '--run-id', env['GSD_RUN_ID'],
        '--dispatch-limit', '3', '--token-limit', '1000']
    tail = (['--selection-manifest', env['FFS_SELECTION_MANIFEST'], '--', '/gsd-plan-phase', '1']
            if entry == 'managed-start' else ['--frontend', 'fix', '--select-file', 'src/selected.sh'])

    def invoke(capacity, revision, key, *, resume=False):
        policy = {'worker_capacity': capacity, 'expected_revision': revision, 'request_key': key}
        result = subprocess.run(argv + ['--capacity-policy', json.dumps(policy)]
            + (['--resume', '--request-key', 'capacity-resume'] if resume else []) + tail, cwd=primary, env=env,
            capture_output=True, text=True, timeout=60)
        return result, json.loads(result.stdout)

    first, body = invoke(1, 0, 'capacity-one')
    assert first.returncode == 78 and body['code'] == 'HOST_CAPABILITY_UNQUALIFIED', (body, first.stderr)
    store = ControlStore(authority / 'control.sqlite3')

    def snapshot():
        with store.read_transaction() as tx:
            limits = dict(tx.execute('SELECT * FROM authority_run_limits').fetchone())
            request = tx.execute('SELECT request_digest FROM context_requests WHERE request_key=?',
                                 (env['FFS_REQUEST_KEY'],)).fetchone()[0]
        return limits, request

    initial = snapshot()
    assert store.get_capacity_policy(repository_id=repository_id, run_id=env['GSD_RUN_ID'])['worker_capacity'] == 1
    second, body = invoke(2, 1, 'capacity-two', resume=True)
    assert second.returncode == 78 and body['code'] == 'HOST_CAPABILITY_UNQUALIFIED', (body, second.stderr)
    policy = store.get_capacity_policy(repository_id=repository_id, run_id=env['GSD_RUN_ID'])
    assert policy['worker_capacity'] == 2 and policy['revision'] == 2
    assert snapshot() == initial
    replay, body = invoke(1, 0, 'capacity-one', resume=True)
    assert replay.returncode == 78 and body['code'] == 'HOST_CAPABILITY_UNQUALIFIED', (body, replay.stderr)
    assert store.get_capacity_policy(repository_id=repository_id, run_id=env['GSD_RUN_ID'])['revision'] == 2
    assert snapshot() == initial
    budget = store.get_run_policy_budget(repository_id=repository_id, run_id=env['GSD_RUN_ID'])
    assert budget.launch_charged == 0 and budget.tier == 'medium'
