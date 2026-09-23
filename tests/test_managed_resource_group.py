"""Scripted native PIDs prove envelope reuse; this is not model qualification."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import pytest

from run_state.managed_admission import ManagedAdmissionQueue
from run_state.managed_resource_group import ManagedParentResourceCoordinator
from run_state.prelaunch_inventory import freeze_prelaunch_plan_inventory
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.upstream import UpstreamRuntime
from run_state.workspace import inspect_workspace, begin_child_workspace_preparation, prepare_workspace, load_input_snapshot
from test_supervised_process import setup_owner


@pytest.mark.parametrize('mode', ['safe', 'unsafe', 'release-crash'])
def test_actual_parent_and_sequential_children_share_one_prepaid_envelope(tmp_path, monkeypatch, mode):
    unsafe = mode == 'unsafe'
    supervisor, store, parent = setup_owner(tmp_path)
    token = supervisor.token
    store.transition_activity(token, parent.activity_id, expected='pending', new='active', reason='fixture parent')
    with store.read_transaction() as tx:
        preparation_id = tx.execute('SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?',
                                    (parent.activity_id,)).fetchone()[0]
    ready = inspect_workspace(store, preparation_id)
    phases = ready.path / '.planning' / 'phases' / '01-fixture'
    phases.mkdir(parents=True)
    for index in (1, 2):
        (phases / f'01-0{index}-PLAN.md').write_text(f'---\nphase: 01\nplan: 0{index}\n---\nPlan\n')
    runtime = UpstreamRuntime.from_manifest(json.loads(Path(os.environ['FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR']).read_bytes()))
    queue = ManagedAdmissionQueue(tmp_path / 'shared', observation_provider=lambda:
        ResourceObservation(time.monotonic_ns(), 2, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
    original = SharedResourceCoordinator(store, token, queue=queue)
    supervisor.shared_resource_coordinator = original
    supervisor.resource_demand_policy = cold_start_demand
    with store.transaction() as tx:
        tx.execute('UPDATE context_runs SET planning_scope=? WHERE repository_id=? AND run_id=?',
                   ('1', token.repository_id, token.run_id))
        root = tx.execute('SELECT activity_id,workspace FROM context_runs WHERE repository_id=? AND run_id=?',
                          (token.repository_id, token.run_id)).fetchone()
    context = SimpleNamespace(activity_id=root['activity_id'], workspace=root['workspace'],
        upstream={'runtime_digest': runtime.runtime_digest,
                  'planning_root': str(Path(root['workspace']) / '.planning')})
    supervisor.configure_managed_parent_resources(parent, context, ready, runtime)
    coordinator = supervisor.shared_resource_coordinator
    assert isinstance(coordinator, ManagedParentResourceCoordinator)
    assert len(coordinator.inventory['plans']) == 2
    parent = replace(parent, command=(sys.executable, '-c',
        "import time; from pathlib import Path; end=time.monotonic()+30\nwhile not Path('release').exists():\n if time.monotonic()>end: raise RuntimeError('deadline')\n time.sleep(.01)"))
    handle = supervisor.launch(parent)
    assert coordinator.child_width == 1
    assert len(queue.snapshot()) == 2
    root_snapshot = load_input_snapshot(store, ready)
    for index in (1, 2):
        pending = begin_child_workspace_preparation(store, token, parent_activity_id=parent.activity_id,
            request_key=f'nested-{index}', role='worker', base_commit=ready.base_commit,
            selected_input_manifest=root_snapshot.manifest, repository_path=ready.repository_path)
        child_ready = prepare_workspace(store, token, pending, input_snapshot=root_snapshot)
        child = store.create_child_activity(token, parent_activity_id=parent.activity_id, role='worker',
            request_key=f'nested-activity-{index}', candidate_hash=child_ready.input_digest,
            contract_hash=parent.contract_hash, runtime_identity=parent.runtime_identity,
            workspace_binding=str(child_ready.path), workspace_preparation_id=child_ready.id, retry_budget=1)
        request = replace(parent, activity_id=child.id, request_key=f'nested-launch-{index}',
            workspace=str(child_ready.path), command=(sys.executable, '-c', 'print("nested")'))
        if unsafe:
            from run_state.supervisor import SupervisorRefused
            queue._observe = lambda: ResourceObservation(time.monotonic_ns(), 0, 0, 4 << 30, 100, 100, {}, 'pressure-fixture')
            with pytest.raises(SupervisorRefused, match='RESOURCE_GROUP_PHYSICAL_UNSAFE'):
                supervisor.launch(request)
            handle.process.wait(timeout=5)
            assert store.get_activity(parent.activity_id).state == 'failed'
            with store.read_transaction() as tx:
                assert tx.execute('SELECT dispatch_used FROM authority_run_limits').fetchone()[0] == 2
                assert tx.execute('SELECT COUNT(*) FROM authority_launch_intents WHERE child_pid IS NOT NULL').fetchone()[0] == 1
            return
        child_handle = supervisor.launch(request)
        if mode == 'release-crash' and index == 1:
            _finish_with_release_crash(supervisor, coordinator, child_handle, monkeypatch, 'release')
        else:
            assert supervisor.finish(child_handle, timeout=10, token_usage=0)['returncode'] == 0
        assert len(queue.snapshot()) == 2
        assert all(row['status'] == 'active' for row in queue.snapshot())
    (ready.path / 'release').touch()
    if mode == 'release-crash':
        # Crash between mark_parent_ended and close: the replay must finish the close.
        _finish_with_release_crash(supervisor, coordinator.registry, handle, monkeypatch, 'close_after_parent_end')
    else:
        assert supervisor.finish(handle, timeout=10, token_usage=0)['returncode'] == 0
    assert all(row['status'] == 'released' for row in queue.snapshot())
    with store.read_transaction() as tx:
        assert tx.execute('SELECT dispatch_used FROM authority_run_limits').fetchone()[0] == 3


def _finish_with_release_crash(supervisor, target, handle, monkeypatch, method):
    """Completion recorded, release crashed: a same-owner replay recovers and releases the slot."""
    coordinator = supervisor.shared_resource_coordinator
    with monkeypatch.context() as crash:
        def interrupted(*args, **kwargs):
            raise RuntimeError('fixture crash')
        crash.setattr(target, method, interrupted)
        with pytest.raises(RuntimeError, match='fixture crash'):
            supervisor.finish(handle, timeout=10, token_usage=0)
    reservation = coordinator.restore_bound_intent(handle.intent_id)
    assert reservation is not None
    coordinator.release(reservation)
    coordinator.release(reservation)  # a second replay is a no-op, never a refusal
    with monkeypatch.context() as stale:  # a concurrent replay settled the slot after this one read it
        stale.setattr(coordinator, '_slot_released', lambda *args: False)
        coordinator.release(reservation)
    assert coordinator.restore_bound_intent(handle.intent_id) is None


@pytest.mark.parametrize('replay', [False, True])
def test_complete_wave_is_chunked_inside_frozen_parent_envelope(tmp_path, monkeypatch, replay):
    from run_state.gsd_wave_bridge import persist_manifest
    from test_wave_consumer import wave_fixture
    owner = setup_owner(tmp_path)
    supervisor, store, _request = owner
    with store.transaction() as tx:
        tx.execute('UPDATE authority_run_limits SET dispatch_limit=12')
        tx.execute("UPDATE context_runs SET writer_version='ffs-supervisor/1'")
    queue = ManagedAdmissionQueue(tmp_path / 'shared', observation_provider=lambda:
        ResourceObservation(time.monotonic_ns(), 2, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))

    def configure(request):
        assert store.get_activity(request.activity_id).state == 'active'
        with store.read_transaction() as tx:
            prep_id = tx.execute('SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?',
                                 (request.activity_id,)).fetchone()[0]
        ready = inspect_workspace(store, prep_id)
        phase = ready.path / '.planning' / 'phases' / '01-fixture'
        phase.mkdir(parents=True)
        for index in range(2):
            (phase / f'01-0{index + 1}-PLAN.md').write_text(
                f'---\nphase: 01\nplan: 0{index + 1}\nfiles_modified: [result-{index}.txt]\ndepends_on: []\n---\nPlan\n')
        runtime = UpstreamRuntime.from_manifest(json.loads(Path(os.environ['FFS_TEST_UPSTREAM_RUNTIME_DESCRIPTOR']).read_bytes()))
        inventory, digest = freeze_prelaunch_plan_inventory(store, supervisor.token, ready,
            activity_id=request.activity_id, runtime_identity=request.runtime_identity, runtime=runtime,
            phase_directory=phase, evidence_root=supervisor.evidence_root, request_key='fixture-wave-plans')
        supervisor.shared_resource_coordinator = ManagedParentResourceCoordinator(
            SharedResourceCoordinator(store, supervisor.token, queue=queue), parent_request=request,
            inventory=inventory, inventory_hash=digest, demand=cold_start_demand(replace(request, monitor_result=True)))
        supervisor.resource_demand_policy = cold_start_demand

    with wave_fixture(tmp_path, monkeypatch, owner=owner, record=False, before_launch=configure) as fixture:
        for index, plan in enumerate(fixture.manifest['plans']):
            plan['id'] = f'01-0{index + 1}'
        raw = json.dumps(fixture.manifest, sort_keys=True, separators=(',', ':')).encode()
        locator, digest = persist_manifest(raw, fixture.parent)
        fixture.message['body'] = {'manifest_locator': locator, 'manifest_sha256': digest}
        event = fixture.channel._request(fixture.outer.identity, fixture.message)['event_id']
        if replay:
            from run_state.wave_consumer import WaveConsumer

            def interrupted(*_args, **_kwargs):
                raise KeyboardInterrupt('all original chunks finished; publication interrupted')

            monkeypatch.setattr(fixture.consumer, '_settle_wave', interrupted)
            with pytest.raises(KeyboardInterrupt):
                fixture.consumer(event)
            consumer = WaveConsumer(supervisor, lambda *_: pytest.fail('replay prepared a new child'), finish_timeout=15)
            reply = consumer(event)
        else:
            reply = fixture.consumer(event)
        assert [row['status'] for row in reply['results']] == ['complete', 'complete']
        assert all((fixture.parent / f'result-{index}.txt').read_text() == 'done' for index in range(2))
        assert len(queue.snapshot()) == 2
        with store.read_transaction() as tx:
            assert tx.execute('SELECT COUNT(*) FROM authority_launch_cohorts').fetchone()[0] == 2
            assert tx.execute('SELECT dispatch_used FROM authority_run_limits').fetchone()[0] == 3
