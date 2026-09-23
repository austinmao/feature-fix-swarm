"""Actual scripted process transport under shared leases; no native host claim."""
from dataclasses import replace
import time

import pytest

from run_state.managed_admission import ManagedAdmissionQueue
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import ControlStoreRefused
from run_state.supervisor import DispatchRequest, Supervisor
from test_supervised_process import setup_owner, _allocate_registered_child, _receipt_bound_request


@pytest.mark.parametrize('cohort', [False, True])
def test_real_transport_binds_every_lease_and_releases_actual_consumers(tmp_path, cohort):
    supervisor, store, request = setup_owner(tmp_path)
    queue = ManagedAdmissionQueue(tmp_path / 'shared', observation_provider=lambda:
        ResourceObservation(time.monotonic_ns(), 8, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
    supervisor.shared_resource_coordinator = SharedResourceCoordinator(store, supervisor.token, queue=queue)
    supervisor.resource_demand_policy = cold_start_demand
    if cohort:
        child, ready = _allocate_registered_child(store, supervisor.token, key='second')
        second = DispatchRequest(child.id, 'second', request.command, str(ready.path), ready.base_commit,
                                 request.runtime_identity, contract_hash=request.contract_hash)
        requests = tuple(_receipt_bound_request(store, supervisor.token, value)
                         for value in (request, second))
        handles = supervisor.launch_cohort(tuple(replace(value, monitor_result=True) for value in requests),
                                           request_key='pair')
    else:
        handles = (supervisor.launch(request),)
    active = queue.snapshot()
    assert len(active) == len(handles)
    assert {row['launch_intent_id'] for row in active} == {handle.intent_id for handle in handles}
    assert all(row['status'] == 'active' and row['child_pid'] for row in active)
    for handle in handles:
        assert supervisor.finish(handle, timeout=15, token_usage=0)['returncode'] == 0
    assert all(row['status'] == 'released' for row in queue.snapshot())


@pytest.mark.parametrize('mutation', [None, 'generation', 'owner', 'intent', 'consumer', 'demand'])
def test_monitor_resume_restores_only_exact_same_writer_lease(tmp_path, mutation):
    supervisor, store, request = setup_owner(tmp_path)
    queue = ManagedAdmissionQueue(tmp_path / 'shared', observation_provider=lambda:
        ResourceObservation(time.monotonic_ns(), 8, 4 << 30, 4 << 30, 100, 100, {}, 'fixture'))
    coordinator = SharedResourceCoordinator(store, supervisor.token, queue=queue)
    supervisor.shared_resource_coordinator = coordinator
    supervisor.resource_demand_policy = cold_start_demand
    handle = supervisor.launch(replace(request, monitor_result=True))
    handle.process.wait(timeout=15)
    original = queue.snapshot()[0]
    mutations = {'generation': ('generation', original['generation'] + 1),
                 'owner': ('start_token', 'fixture-foreign-owner'),
                 'intent': ('launch_intent_id', 'fixture-foreign-intent'),
                 'consumer': ('child_start_token', 'fixture-foreign-consumer'),
                 'demand': ('demand_json', '{}')}
    if mutation is not None:
        column, value = mutations[mutation]
        with queue._transaction() as tx:
            tx.execute(f'UPDATE managed_admissions SET {column}=? WHERE ticket=?', (value, original['ticket']))
    resumed = Supervisor(store, supervisor.token, evidence_root=supervisor.evidence_root,
                         shared_resource_coordinator=coordinator, resource_demand_policy=cold_start_demand)
    if mutation is None:
        recovered = resumed.resume_monitored(handle.intent_id)
        assert resumed.finish(recovered, timeout=15, token_usage=0)['returncode'] == 0
        assert queue.snapshot()[0]['status'] == 'released'
    else:
        with pytest.raises(ControlStoreRefused, match='SHARED_RESOURCE_LEASE_RESTORE_REQUIRED'):
            resumed.resume_monitored(handle.intent_id)
        assert queue.snapshot()[0]['status'] == 'active'
