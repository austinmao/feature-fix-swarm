"""Successor owner settles, never adopts, a SIGKILLed predecessor's prepaid group.

Scripted native PIDs and a fixture resource observation; not model or host qualification.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from process_identity import ProcessIdentity
from run_state.ownership import StartRequest, reserve_resources
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.managed_resource_group import settle_predecessor_groups
from run_state.resource_observation import ResourceObservation
from run_state.state import ControlStore
from run_state.supervisor import Supervisor, SupervisorRefused

_OWNER = """
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json, os, sys, time
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.upstream import UpstreamRuntime
from run_state.workspace import inspect_workspace
from test_supervised_process import setup_owner
root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=True)
supervisor, store, parent = setup_owner(root)
token = supervisor.token
store.transition_activity(token, parent.activity_id, expected='pending', new='active', reason='fixture parent')
with store.read_transaction() as tx:
    preparation_id = tx.execute('SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?',
                                (parent.activity_id,)).fetchone()[0]
ready = inspect_workspace(store, preparation_id)
phases = ready.path / '.planning' / 'phases' / '01-fixture'
phases.mkdir(parents=True)
(phases / '01-01-PLAN.md').write_text('---\\nphase: 01\\nplan: 01\\n---\\nPlan\\n')
runtime = UpstreamRuntime.from_manifest(json.loads(Path(os.environ['FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR']).read_bytes()))
queue = ManagedAdmissionQueue(Path(sys.argv[3]), observation_provider=lambda:
    ResourceObservation(time.monotonic_ns(), 2, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
supervisor.shared_resource_coordinator = SharedResourceCoordinator(store, token, queue=queue)
supervisor.resource_demand_policy = cold_start_demand
with store.transaction() as tx:
    tx.execute('UPDATE context_runs SET planning_scope=? WHERE repository_id=? AND run_id=?',
               ('1', token.repository_id, token.run_id))
    run = tx.execute('SELECT activity_id,workspace FROM context_runs WHERE repository_id=? AND run_id=?',
                     (token.repository_id, token.run_id)).fetchone()
context = SimpleNamespace(activity_id=run['activity_id'], workspace=run['workspace'],
    upstream={'runtime_digest': runtime.runtime_digest, 'planning_root': str(Path(run['workspace']) / '.planning')})
supervisor.configure_managed_parent_resources(parent, context, ready, runtime)
if sys.argv[4] == 'staging':
    # Fixture pressure after width selection leaves a real, wholly unlaunched staging group.
    reserve = supervisor.shared_resource_coordinator.registry.reserve
    def reserve_staging(plan):
        queue._observe = lambda: ResourceObservation(
            time.monotonic_ns(), 0, 4 << 30, 4 << 30, 100, 100, {}, 'fixture')
        reservation = reserve(replace(plan, staging_expires_ns=time.monotonic_ns() - 1))
        assert reservation.state == 'staging'
        Path(sys.argv[2]).write_text(json.dumps({'db': str(store.db_path), 'generation': token.generation}))
        while True: time.sleep(1)
    supervisor.shared_resource_coordinator.registry.reserve = reserve_staging
    supervisor.shared_resource_coordinator._reserve_group()
if sys.argv[4] == 'held':
    # Reserve and hold for launch, then stall before any intent or parent binding exists.
    supervisor.shared_resource_coordinator._reserve_group()
    Path(sys.argv[2]).write_text(json.dumps({'db': str(store.db_path), 'generation': token.generation}))
    while True: time.sleep(1)
handle = supervisor.launch(replace(parent, monitor_result=True, command=(sys.executable, '-c',
    "import time; from pathlib import Path\\nwhile not Path('release').exists(): time.sleep(.01)")))
extra = {}
if sys.argv[4] == 'child':
    from run_state.workspace import begin_child_workspace_preparation, load_input_snapshot, prepare_workspace
    snapshot = load_input_snapshot(store, ready)
    pending = begin_child_workspace_preparation(store, token, parent_activity_id=parent.activity_id,
        request_key='nested', role='worker', base_commit=ready.base_commit,
        selected_input_manifest=snapshot.manifest, repository_path=ready.repository_path)
    nested_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
    nested = store.create_child_activity(token, parent_activity_id=parent.activity_id, role='worker',
        request_key='nested-activity', candidate_hash=nested_ready.input_digest, contract_hash=parent.contract_hash,
        runtime_identity=parent.runtime_identity, workspace_binding=str(nested_ready.path),
        workspace_preparation_id=nested_ready.id, retry_budget=1)
    nested_handle = supervisor.launch(replace(parent, activity_id=nested.id, request_key='nested-launch',
        workspace=str(nested_ready.path), monitor_result=True, command=(sys.executable, '-c',
        "import time; from pathlib import Path\\nwhile not Path('release').exists(): time.sleep(.01)")))
    extra = {'child_intent_id': nested_handle.intent_id, 'child_workspace': str(nested_ready.path)}
Path(sys.argv[2]).write_text(json.dumps({**extra, 'db': str(store.db_path), 'intent_id': handle.intent_id,
    'evidence_root': str(supervisor.evidence_root), 'workspace': str(ready.path), 'generation': token.generation}))
while True: time.sleep(1)
"""


@pytest.mark.parametrize('child', [False, True], ids=['parent-only', 'active-child-claim'])
def test_successor_settles_predecessor_group_without_adopting_tickets(tmp_path, child):
    metadata, shared = tmp_path / 'owner.json', tmp_path / 'shared'
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'lib') + os.pathsep + str(Path(__file__).parent)
    owner = subprocess.Popen([sys.executable, '-c', _OWNER, str(tmp_path / 'owner'), str(metadata), str(shared),
                              'child' if child else 'none'],
                             env=environment)
    payload = None
    try:
        deadline = time.monotonic() + 60
        while not metadata.exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(.02)
        payload = json.loads(metadata.read_text())
        owner.kill()
        assert owner.wait(timeout=10) != 0
        store = ControlStore(Path(payload['db']))
        with store.read_transaction() as tx:
            run = tx.execute('SELECT * FROM context_runs').fetchone()
        successor = reserve_resources(store, StartRequest(
            run['run_id'], run['workspace'], run['objective_digest'], ProcessIdentity.current(),
            repository_id=run['repository_id'], planning_scope=run['planning_scope'])).token
        assert successor.generation > payload['generation']
        queue = ManagedAdmissionQueue(shared, observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 2, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
        before = queue.snapshot()
        assert len(before) == 2 and all(row['status'] == 'active' for row in before)
        # Live native parent, unsettled intent: nothing is released or adopted.
        retained = settle_predecessor_groups(store, successor, queue)
        assert [item['state'] for item in retained] == ['retained'] and retained[0]['generation'] == payload['generation']
        assert queue.snapshot() == before
        with queue._connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM resource_parent_group_claims WHERE state='active'"
                                      ).fetchone()[0] == int(child)
        resumed = Supervisor(store, successor, evidence_root=Path(payload['evidence_root']))
        pending = [(payload[key], payload[place]) for key, place in
                   (('child_intent_id', 'child_workspace'), ('intent_id', 'workspace')) if key in payload]
        for position, (intent_id, workspace) in enumerate(pending):
            handle = resumed.resume_monitored(intent_id)
            (Path(workspace) / 'release').touch()
            deadline = time.monotonic() + 30
            while True:
                try:
                    assert resumed.finish(handle, token_usage=0)['returncode'] == 0
                    break
                except SupervisorRefused as error:
                    assert error.code == 'MONITOR_RESULT_PENDING' and time.monotonic() < deadline
                    time.sleep(.02)
            if position + 1 < len(pending):
                # Child settled, native parent still alive: the group stays held.
                assert [item['state'] for item in settle_predecessor_groups(store, successor, queue)] == ['retained']
        # The settled child's claim is still active until settlement finishes it; nothing else released it.
        with queue._connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM resource_parent_group_claims WHERE state='active'").fetchone()[0] == int(child)
        closed = settle_predecessor_groups(store, successor, queue)
        assert [item['state'] for item in closed] == ['closed']
        with queue._connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM resource_parent_group_claims WHERE state='active'").fetchone()[0] == 0
        after = queue.snapshot()
        assert all(row['status'] == 'released' for row in after)
        assert [(row['sequence'], row['ticket']) for row in after] == [(row['sequence'], row['ticket']) for row in before]
        assert settle_predecessor_groups(store, successor, queue) == []
        # The successor reserves its own group at its own generation through the ordinary coordinator.
        from dataclasses import replace
        from types import SimpleNamespace
        from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
        from run_state.supervisor import DispatchRequest
        from run_state.upstream import UpstreamRuntime
        from test_supervised_process import _allocate_registered_child
        # Same successor rebinding the CLI performs: revalidate the READY fence, move the run activity.
        from run_state.cli import _refresh_parent_activity_generation
        from run_state.workspace import revalidate_ready_fence
        revalidate_ready_fence(store, successor, run['preparation_id'])
        _refresh_parent_activity_generation(store, successor, run['activity_id'])
        child, ready = _allocate_registered_child(store, successor, key='successor-parent')
        store.transition_activity(successor, child.id, expected='pending', new='active', reason='fixture parent')
        phases = ready.path / '.planning' / 'phases' / '01-fixture'
        phases.mkdir(parents=True)
        (phases / '01-01-PLAN.md').write_text('---\nphase: 01\nplan: 01\n---\nPlan\n')
        runtime = UpstreamRuntime.from_manifest(json.loads(Path(os.environ['FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR']).read_bytes()))
        resumed.shared_resource_coordinator = SharedResourceCoordinator(store, successor, queue=queue)
        resumed.resource_demand_policy = cold_start_demand
        context = SimpleNamespace(activity_id=run['activity_id'], workspace=run['workspace'], upstream={
            'runtime_digest': runtime.runtime_digest, 'planning_root': str(Path(run['workspace']) / '.planning')})
        request = DispatchRequest(child.id, 'successor', (sys.executable, '-c', 'print(1)'), str(ready.path),
                                  ready.base_commit, 'b' * 64, contract_hash='d' * 64)
        resumed.configure_managed_parent_resources(request, context, ready, runtime)
        fresh = resumed.launch(replace(request, monitor_result=True))
        with queue._connection() as connection:
            groups = [json.loads(row['plan_json']) for row in connection.execute(
                "SELECT plan_json FROM resource_parent_groups WHERE state='reserved'")]
        assert [group['generation'] for group in groups] == [successor.generation]
        old = {row['ticket'] for row in before}
        assert [row for row in queue.snapshot() if row['status'] == 'active' and row['ticket'] in old] == []
        assert any(row['status'] == 'active' for row in queue.snapshot())
        deadline = time.monotonic() + 30
        while True:
            try:
                assert resumed.finish(fresh, token_usage=0)['returncode'] == 0
                break
            except SupervisorRefused as error:
                assert error.code == 'MONITOR_RESULT_PENDING' and time.monotonic() < deadline
                time.sleep(.02)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if payload is not None:
            (Path(payload['workspace']) / 'release').touch()
            if 'child_workspace' in payload:
                (Path(payload['child_workspace']) / 'release').touch()


def test_successor_expires_predecessor_killed_while_staging(tmp_path):
    """Scripted native owner PID and fixture pressure/expiry; no host or model qualification."""
    metadata, shared = tmp_path / 'owner.json', tmp_path / 'shared'
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'lib') + os.pathsep + str(Path(__file__).parent)
    owner = subprocess.Popen([sys.executable, '-c', _OWNER, str(tmp_path / 'owner'), str(metadata), str(shared), 'staging'],
                             env=environment)
    try:
        deadline = time.monotonic() + 60
        while not metadata.exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(.02)
        payload = json.loads(metadata.read_text())
        store = ControlStore(Path(payload['db']))
        queue = ManagedAdmissionQueue(shared, observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 2, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
        before = queue.snapshot()
        assert len(before) == 2 and all(row['status'] == 'waiting' for row in before)
        assert all(row['pid'] == owner.pid and row['generation'] == payload['generation'] for row in before)
        with queue._connection() as connection:
            group = dict(connection.execute('SELECT * FROM resource_parent_groups').fetchone())
            slots = [dict(row) for row in connection.execute('SELECT * FROM resource_parent_group_slots ORDER BY slot_id')]
        assert group['state'] == 'staging' and group['parent_binding_json'] is None
        assert group['expires_ns'] < time.monotonic_ns()
        assert len(slots) == len(before)
        assert all(slot['ever_launched'] == 0 and slot['binding_json'] is None for slot in slots)
        with store.read_transaction() as tx:
            run = tx.execute('SELECT * FROM context_runs').fetchone()
            assert tx.execute('SELECT COUNT(*) FROM authority_launch_intents').fetchone()[0] == 0
        assert owner.poll() is None
        owner.kill()
        assert owner.wait(timeout=10) != 0
        assert queue.snapshot() == before
        successor = reserve_resources(store, StartRequest(
            run['run_id'], run['workspace'], run['objective_digest'], ProcessIdentity.current(),
            repository_id=run['repository_id'], planning_scope=run['planning_scope'])).token
        assert successor.generation > payload['generation']
        assert settle_predecessor_groups(store, successor, queue) == [{
            'group_id': group['group_id'], 'generation': payload['generation'], 'state': 'expired'}]
        with queue._connection() as connection:
            after_group = dict(connection.execute('SELECT * FROM resource_parent_groups').fetchone())
            after_slots = [dict(row) for row in connection.execute('SELECT * FROM resource_parent_group_slots ORDER BY slot_id')]
        assert after_group == {**group, 'state': 'expired'}
        assert after_slots == slots
        released = queue.snapshot()
        assert released == [{**row, 'status': 'released'} for row in before]
        assert settle_predecessor_groups(store, successor, queue) == []
        assert queue.snapshot() == released
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


def test_successor_closes_held_never_bound_group_only_on_authority_proof(tmp_path):
    metadata, shared = tmp_path / 'owner.json', tmp_path / 'shared'
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'lib') + os.pathsep + str(Path(__file__).parent)
    owner = subprocess.Popen([sys.executable, '-c', _OWNER, str(tmp_path / 'owner'), str(metadata), str(shared), 'held'],
                             env=environment)
    try:
        deadline = time.monotonic() + 60
        while not metadata.exists():
            assert owner.poll() is None and time.monotonic() < deadline
            time.sleep(.02)
        payload = json.loads(metadata.read_text())
        owner.kill()
        assert owner.wait(timeout=10) != 0
        store = ControlStore(Path(payload['db']))
        with store.read_transaction() as tx:
            run = tx.execute('SELECT * FROM context_runs').fetchone()
            assert tx.execute('SELECT COUNT(*) FROM authority_launch_intents').fetchone()[0] == 0
        successor = reserve_resources(store, StartRequest(
            run['run_id'], run['workspace'], run['objective_digest'], ProcessIdentity.current(),
            repository_id=run['repository_id'], planning_scope=run['planning_scope'])).token
        queue = ManagedAdmissionQueue(shared, observation_provider=lambda:
            ResourceObservation(time.monotonic_ns(), 2, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
        before = queue.snapshot()
        assert len(before) == 2 and all(row['status'] == 'active' for row in before)
        with queue._connection() as connection:
            group = connection.execute('SELECT state,parent_binding_json FROM resource_parent_groups').fetchone()
            assert (group['state'], group['parent_binding_json']) == ('reserved', None)
            assert connection.execute("SELECT ever_launched FROM resource_parent_group_slots WHERE role='parent'").fetchone()[0] == 1
        # An intent of the fenced generation that authority does not show never-authorized keeps the hold.
        with store.transaction() as tx:
            activity = tx.execute("SELECT activity_id FROM authority_child_bindings").fetchone()[0]
            tx.execute("INSERT INTO authority_launch_intents(id,activity_id,attempt_ordinal,state,generation,created_at,updated_at) "
                       "VALUES('fixture-intent',?,1,'authorized',?,'t','t')", (activity, payload['generation']))
        retained = settle_predecessor_groups(store, successor, queue)
        assert [(item['state'], item['code']) for item in retained] == [('retained', 'RESOURCE_GROUP_PRELAUNCH_PROOF_REQUIRED')]
        assert queue.snapshot() == before
        with store.transaction() as tx:
            tx.execute("DELETE FROM authority_launch_intents WHERE id='fixture-intent'")
        assert [item['state'] for item in settle_predecessor_groups(store, successor, queue)] == ['closed']
        assert all(row['status'] == 'released' for row in queue.snapshot())
        assert settle_predecessor_groups(store, successor, queue) == []
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
