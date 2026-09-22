from __future__ import annotations

import sqlite3
import time

import pytest

from process_identity import ProcessIdentity
from run_state import managed_admission
from run_state.managed_admission import ManagedAdmissionQueue, ManagedAdmissionRefused
from run_state.resource_observation import ResourceDemand, ResourceObservation


OWNER = ProcessIdentity("fixture-host", "fixture-boot", 7, "fixture-start")


def _observation(cpu=2):
    return ResourceObservation(time.monotonic_ns(), cpu, 1 << 30, 1 << 30, 2, 100, {})


def _queue(tmp_path, monkeypatch, cpu=2):
    monkeypatch.setattr(
        managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER)
    )
    monkeypatch.setattr(managed_admission, "probe_identity", lambda _: "LIVE")
    return ManagedAdmissionQueue(
        tmp_path / "admission", observation_provider=lambda: _observation(cpu)
    )


def test_dispatch_lease_replay_preserves_age_and_binding(tmp_path, monkeypatch):
    from dataclasses import replace
    from run_state.managed_admission import LeaseIdentity
    queue = _queue(tmp_path, monkeypatch)
    request = dict(state_root=tmp_path, run_id="run", repository_id="repo",
                   request_key="request", generation=2, demand=ResourceDemand(cpu=1))
    ticket = queue.enqueue(**request)
    original = queue.status(ticket)
    assert queue.enqueue(**request) == ticket
    assert queue.status(ticket)["group_age_ns"] == original["group_age_ns"]
    assert len(queue.snapshot()) == 1
    with pytest.raises(ManagedAdmissionRefused, match="LEASE_RECONCILIATION_REQUIRED"):
        queue.enqueue(**{**request, "demand": ResourceDemand(cpu=2)})
    assert queue.try_admit(ticket)
    identity = LeaseIdentity("repo", "run", "request", "intent", 2, OWNER)
    queue.bind_consumer(ticket, identity)
    queue.bind_consumer(ticket, identity)
    for forged in (replace(identity, request_key="other"), replace(identity, run_id="other"),
                   replace(identity, generation=3), replace(identity, launch_intent_id="other")):
        with pytest.raises(ManagedAdmissionRefused, match="LEASE_IDENTITY_MISMATCH"):
            queue.bind_consumer(ticket, forged)
    assert queue.status(ticket)["launch_intent_id"] == "intent"


@pytest.mark.parametrize("capacity", [2, 4, 8])
def test_fair_round_robin_uses_resource_reservations_not_fixed_capacity(
    tmp_path, monkeypatch, capacity
):
    queue = _queue(tmp_path, monkeypatch, capacity)
    tickets = [
        queue.enqueue(
            state_root=tmp_path / str(index),
            run_id=str(index // 2),
            repository_id="repo",
            demand=ResourceDemand(cpu=1),
        )
        for index in range(capacity + 2)
    ]
    admitted = []
    # One eligible item per fair sweep means an earlier same-run item may wait
    # for the next turn; repeat sweeps without changing capacity.
    while len(admitted) < capacity:
        for ticket in tickets:
            if ticket not in admitted and queue.try_admit(ticket):
                admitted.append(ticket)
    assert len(admitted) == capacity
    assert queue.status(tickets[-1])["active_count"] == capacity


def test_v1_migration_preserves_inode_fences_raw_enqueue_and_allows_release(
    tmp_path, monkeypatch
):
    root = tmp_path / "admission"
    root.mkdir(mode=0o700)
    path = root / "admission.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE admission_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1),capacity INTEGER NOT NULL CHECK(capacity=2))"
        )
        connection.execute(
            "CREATE TABLE managed_admissions (sequence INTEGER PRIMARY KEY AUTOINCREMENT,ticket TEXT NOT NULL UNIQUE,state_root TEXT NOT NULL,run_id TEXT NOT NULL,host_id TEXT NOT NULL,boot_id TEXT NOT NULL,pid INTEGER NOT NULL CHECK(pid>0),start_token TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('waiting','active','released','reclaimed')))"
        )
        connection.execute("INSERT INTO admission_policy VALUES(1,1,2)")
        connection.execute(
            "INSERT INTO managed_admissions(ticket,state_root,run_id,host_id,boot_id,pid,start_token,status) VALUES('old',?,'run',?,?,?,?, 'waiting')",
            (str(tmp_path), OWNER.host_id, OWNER.boot_id, OWNER.pid, OWNER.start_token),
        )
    path.chmod(0o600)
    inode = path.stat().st_ino
    queue = _queue(tmp_path, monkeypatch)
    assert path.stat().st_ino == inode and queue.snapshot()[0]["writer_version"] == 1
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "INSERT INTO managed_admissions(ticket,state_root,run_id,host_id,boot_id,pid,start_token,status) VALUES('raw',?,'raw',?,?,?,?, 'waiting')",
                (
                    str(tmp_path),
                    OWNER.host_id,
                    OWNER.boot_id,
                    OWNER.pid,
                    OWNER.start_token,
                ),
            )
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE managed_admissions SET status='active' WHERE ticket='old'"
            )
        connection.execute(
            "UPDATE managed_admissions SET status='released' WHERE ticket='old'"
        )
    assert queue.snapshot()[0]["status"] == "released"
    local = queue.enqueue(state_root=tmp_path / "new", run_id="new")
    assert not queue.try_admit(local), "legacy release cannot erase opaque demand"


def test_group_demand_is_cumulative_and_busy_provider_does_not_block_local_work(
    tmp_path, monkeypatch
):
    observation = ResourceObservation(
        time.monotonic_ns(), 2, 1 << 30, 1 << 30, 2, 100, {"slow": 0}
    )
    queue = _queue(tmp_path, monkeypatch)
    queue._observe = lambda: observation
    group = [
        queue.enqueue(
            state_root=tmp_path / name,
            run_id="group",
            repository_id="repo",
            demand=ResourceDemand(cpu=2),
            group_id="overlap",
            group_width=2,
        )
        for name in ("one", "two")
    ]
    local = queue.enqueue(
        state_root=tmp_path / "local",
        run_id="local",
        repository_id="repo",
        demand=ResourceDemand(cpu=1),
    )
    assert not queue.try_admit(group[0])
    assert queue.try_admit(local)

    queue = _queue(tmp_path / "provider", monkeypatch)
    queue._observe = lambda: observation
    throttled = queue.enqueue(
        state_root=tmp_path / "throttled",
        run_id="throttled",
        repository_id="repo",
        demand=ResourceDemand(cpu=0, processes=0, provider="slow", provider_units=1),
    )
    feasible = queue.enqueue(
        state_root=tmp_path / "feasible",
        run_id="feasible",
        repository_id="repo",
        demand=ResourceDemand(cpu=1),
    )
    assert not queue.try_admit(throttled)
    assert queue.try_admit(feasible)


def test_descendant_unknown_retains_lease_and_prespawn_requires_control_proof(
    tmp_path, monkeypatch
):
    queue = _queue(tmp_path, monkeypatch)
    ticket = queue.enqueue(
        state_root=tmp_path, run_id="run", repository_id="repo", request_key="request"
    )
    assert queue.try_admit(ticket)
    lease = managed_admission.LeaseIdentity(
        "repo",
        "run",
        "request",
        "intent",
        1,
        OWNER,
        ProcessIdentity("other", "boot", 8, "child"),
    )
    queue.bind_consumer(ticket, lease)
    with pytest.raises(ManagedAdmissionRefused, match="DESCENDANT_RETAINED"):
        queue.release(ticket)
    with pytest.raises(
        ManagedAdmissionRefused, match="PRESPAWN_RECLAIM_PROOF_REQUIRED"
    ):
        queue.reclaim_pre_spawn(ticket, lease, object())


def test_aged_feasible_group_protects_released_capacity_from_renewal(tmp_path, monkeypatch):
    queue = _queue(tmp_path, monkeypatch)
    active = queue.enqueue(state_root=tmp_path, run_id='active', repository_id='repo',
                           request_key='active-request', demand=ResourceDemand(cpu=1))
    assert queue.try_admit(active)
    child = ProcessIdentity(OWNER.host_id, OWNER.boot_id, 8, 'finished-child')
    queue.bind_consumer(active, managed_admission.LeaseIdentity(
        'repo', 'active', 'active-request', 'active-intent', 1, OWNER, child))
    group = [queue.enqueue(state_root=tmp_path, run_id='group', repository_id='repo',
                           request_key=f'member-{i}', demand=ResourceDemand(cpu=1),
                           group_id='aged', group_width=2) for i in range(2)]
    renewal = queue.enqueue(state_root=tmp_path, run_id='small', demand=ResourceDemand(cpu=1))
    with queue._transaction() as tx:
        tx.execute("UPDATE managed_admissions SET group_age_ns=? WHERE group_id='aged'",
                   (time.monotonic_ns() - 10_000_000_000,))
    assert not queue.try_admit(group[0])
    assert not queue.try_admit(renewal)
    assert queue.status(renewal)['limiting_resource'] == 'aged-group:aged'
    monkeypatch.setattr(managed_admission, 'probe_identity', lambda identity: 'DEAD' if identity == child else 'LIVE')
    queue.release(active)
    assert queue.try_admit(group[0])
    assert all(queue.status(ticket)['status'] == 'active' for ticket in group)


def test_aged_provider_group_does_not_capture_unrelated_provider(tmp_path, monkeypatch):
    queue = _queue(tmp_path, monkeypatch)
    queue._observe = lambda: ResourceObservation(time.monotonic_ns(), 2, 1 << 30, 1 << 30,
                                                 2, 100, {'codex': 2, 'claude': 1})
    demand = ResourceDemand(cpu=0, processes=0, provider='codex', provider_units=1)
    active = queue.enqueue(state_root=tmp_path, run_id='active', demand=demand)
    assert queue.try_admit(active)
    group = [queue.enqueue(state_root=tmp_path, run_id='group', repository_id='repo',
                           request_key=f'member-{i}', demand=demand, group_id='aged', group_width=2)
             for i in range(2)]
    unrelated = queue.enqueue(state_root=tmp_path, run_id='other', demand=ResourceDemand(
        cpu=0, processes=0, provider='claude', provider_units=1))
    with queue._transaction() as tx:
        tx.execute("UPDATE managed_admissions SET group_age_ns=? WHERE group_id='aged'",
                   (time.monotonic_ns() - 10_000_000_000,))
    assert not queue.try_admit(group[0])
    assert queue.try_admit(unrelated)
