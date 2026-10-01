"""F55 + F51 R1-3: a shared admission that never reached a spawned child is released.

F55: the live owner frees the unbound reservations of a launch refused at its authority
reservation (or a reused intent, or a failed bind).  R1-3: an owner that died after the
launch intent committed but before any child process was forked is proven never-spawned
from durable authority (no ``spawn-attempt:`` marker), so the lazy reclaim / successor
settlement frees its capacity.  Launch authority never changes: intents, debits and the
INTENT_RECONCILIATION_REQUIRED refusal stay exactly as the crash left them.

Real ControlStore, Supervisor, ManagedAdmissionQueue and registry; scripted python children
and a fixture resource observation.  Crashes are real ``os._exit`` of a subprocess owner.
Not host, model or capacity-measurement qualification.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from process_identity import ProcessIdentity
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.managed_resource_group import settle_predecessor_groups
from run_state.ownership import OwnershipRefused, StartRequest, reserve_resources
from run_state.resource_groups import ResourceGroupRefused
from run_state.resource_observation import ResourceObservation
from run_state.shared_resources import SharedResourceCoordinator, cold_start_demand
from run_state.state import ControlStore
from run_state.supervisor import DispatchRequest, Supervisor, SupervisorRefused
from run_state.workspace import (
    begin_child_workspace_preparation, inspect_workspace, load_input_snapshot, prepare_workspace,
)
from test_final_review_standalone_lease import _managed_group, _scripted_probe
from test_supervised_process import _allocate_registered_child, _receipt_bound_request, setup_owner

RELEASED_UNBOUND = "owner-released-unbound"
_WAIT = ("import time; from pathlib import Path; end=time.monotonic()+60\n"
         "while not Path('release').exists():\n if time.monotonic()>end: raise RuntimeError('deadline')\n"
         " time.sleep(.01)")


def _observation():
    return ResourceObservation(time.monotonic_ns(), 8, 4 << 30, 4 << 30, 100, 100, {}, "fixture")


def _plain(root, *, fault=None):
    supervisor, store, request = setup_owner(root, fault=fault)
    queue = ManagedAdmissionQueue(root / "shared", observation_provider=_observation)
    supervisor.shared_resource_coordinator = SharedResourceCoordinator(store, supervisor.token, queue=queue)
    supervisor.resource_demand_policy = cold_start_demand
    return supervisor, store, request, queue


def _authority(store):
    with store.read_transaction() as tx:
        return {table: [dict(row) for row in tx.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("authority_launch_intents", "authority_launch_accounting", "authority_run_limits")}


_UNBOUND_RELEASE = ("released", RELEASED_UNBOUND, None, None)


def _lease(row):
    return row["status"], row["limiting_resource"], row["launch_intent_id"], row["child_pid"]


def _set_limits(store, **values):
    with store.transaction() as tx:
        for column, value in values.items():
            tx.execute(f"UPDATE authority_run_limits SET {column}=?", (value,))


# --- F55: the live owner releases an unbound reservation the moment its launch is refused ---------


def test_plain_launch_refused_at_reserve_releases_its_ticket_and_a_same_request_retry_is_readmitted(tmp_path):
    supervisor, store, request, queue = _plain(tmp_path)
    before = _authority(store)
    with pytest.raises(OwnershipRefused, match="TOKEN_LIMIT_EXHAUSTED"):
        supervisor.launch(replace(request, token_reservation=101))
    [row] = queue.snapshot()
    assert _lease(row) == _UNBOUND_RELEASE
    assert _authority(store) == before  # the refusal charged nothing
    # Same generation, same request: the owner's released ticket is re-queued and admitted once.
    handle = supervisor.launch(request)
    assert supervisor.finish(handle, timeout=10, token_usage=0)["returncode"] == 0
    assert [(item["sequence"], item["status"]) for item in queue.snapshot()] == [(row["sequence"], "released")]


def test_qualification_probe_refused_at_reserve_releases_its_standalone_ticket(tmp_path):
    supervisor, store, parent, queue = _plain(tmp_path)
    token = supervisor.token
    store.transition_activity(token, parent.activity_id, expected="pending", new="active", reason="fixture parent")
    with store.read_transaction() as tx:
        preparation_id = tx.execute("SELECT workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?",
                                    (parent.activity_id,)).fetchone()[0]
    ready = inspect_workspace(store, preparation_id)
    request, contract = _scripted_probe(store, token, parent_activity_id=parent.activity_id,
                                        base_commit=ready.base_commit, repository_path=ready.repository_path,
                                        root=tmp_path, key="refused-probe")
    with store.transaction() as tx:  # qualification probes exist only under the managed writer
        tx.execute("UPDATE context_runs SET writer_version='ffs-supervisor/1'")
    _set_limits(store, token_limit=request.token_reservation - 1)
    with pytest.raises(OwnershipRefused, match="TOKEN_LIMIT_EXHAUSTED"):
        supervisor.launch_qualification(request, qualification_contract=contract)
    [row] = queue.snapshot()
    assert _lease(row) == _UNBOUND_RELEASE  # the M5d reviewer-probe shape: no lazy reclaim needed


def test_replayed_launch_of_an_in_flight_intent_releases_only_its_own_unbound_ticket(tmp_path):
    fired = []

    def crash_once(point):
        if point == "after_intent_commit" and not fired:
            fired.append(point)
            raise RuntimeError("injected crash")

    supervisor, store, request, queue = _plain(tmp_path, fault=crash_once)
    with pytest.raises(RuntimeError, match="injected crash"):
        supervisor.launch(request)
    [original] = queue.snapshot()
    with store.read_transaction() as tx:
        [intent] = tx.execute("SELECT * FROM authority_launch_intents").fetchall()
    assert (original["status"], original["launch_intent_id"]) == ("active", intent["id"])
    authority = _authority(store)
    # The identical request resolves the same bound ticket: nothing unbound to release.
    with pytest.raises(SupervisorRefused, match="INTENT_RECONCILIATION_REQUIRED"):
        supervisor.launch(request)
    assert queue.snapshot() == [original]
    # Another request for the in-flight activity takes a new ticket; only that one is released.
    with pytest.raises(SupervisorRefused, match="INTENT_RECONCILIATION_REQUIRED"):
        supervisor.launch(replace(request, request_key="first-replay"))
    first, second = queue.snapshot()
    assert first == original
    assert _lease(second) == _UNBOUND_RELEASE
    assert _authority(store) == authority


def test_refused_cohort_releases_every_member_ticket(tmp_path):
    supervisor, store, request, queue = _plain(tmp_path)
    child, ready = _allocate_registered_child(store, supervisor.token, key="second")
    second = DispatchRequest(child.id, "second", request.command, str(ready.path), ready.base_commit,
                             request.runtime_identity, contract_hash=request.contract_hash)
    requests = tuple(_receipt_bound_request(store, supervisor.token, value) for value in (request, second))
    _set_limits(store, dispatch_limit=1)
    before = _authority(store)
    with pytest.raises(OwnershipRefused, match="DISPATCH_LIMIT_EXHAUSTED"):
        supervisor.launch_cohort(requests, request_key="pair")
    rows = queue.snapshot()
    assert [_lease(row) for row in rows] == [_UNBOUND_RELEASE] * 2
    assert _authority(store) == before


def _nested(store, token, parent, ready, key):
    snapshot = load_input_snapshot(store, ready)
    pending = begin_child_workspace_preparation(
        store, token, parent_activity_id=parent.activity_id, request_key=key + "-workspace", role="worker",
        base_commit=ready.base_commit, selected_input_manifest=snapshot.manifest,
        repository_path=ready.repository_path)
    child_ready = prepare_workspace(store, token, pending, input_snapshot=snapshot)
    child = store.create_child_activity(
        token, parent_activity_id=parent.activity_id, role="worker", request_key=key + "-activity",
        candidate_hash=child_ready.input_digest, contract_hash=parent.contract_hash,
        runtime_identity=parent.runtime_identity, workspace_binding=str(child_ready.path),
        workspace_preparation_id=child_ready.id, retry_budget=1)
    return replace(parent, activity_id=child.id, request_key=key + "-launch", workspace=str(child_ready.path),
                   monitor_result=True, command=(sys.executable, "-c", "print('nested')"))


def _group(queue):
    with queue._connection() as connection:
        group = dict(connection.execute("SELECT * FROM resource_parent_groups").fetchone())
        slots = [dict(row) for row in connection.execute("SELECT * FROM resource_parent_group_slots ORDER BY slot_id")]
        claims = connection.execute("SELECT COUNT(*) FROM resource_parent_group_claims").fetchone()[0]
    return group, slots, claims


def test_managed_child_refused_never_releases_the_shared_parent_ticket_or_group(tmp_path):
    """Guard: a child acquire claims nothing; even an unbound prepaid parent ticket stays held."""
    supervisor, store, parent, ready, queue = _managed_group(tmp_path)
    coordinator = supervisor.shared_resource_coordinator
    coordinator._reserve_group()  # the envelope is held; its parent ticket is not yet bound
    request = _nested(store, supervisor.token, parent, ready, "refused-child")
    tickets, group = queue.snapshot(), _group(queue)
    assert all(row["status"] == "active" and row["launch_intent_id"] is None for row in tickets)
    _set_limits(store, dispatch_limit=0)
    with pytest.raises(OwnershipRefused, match="DISPATCH_LIMIT_EXHAUSTED"):
        supervisor.launch(request)
    assert queue.snapshot() == tickets and _group(queue) == group
    assert coordinator.plan is not None and coordinator._group_state() == "reserved"
    _set_limits(store, dispatch_limit=3)
    handle = supervisor.launch(replace(parent, command=(sys.executable, "-c", "print('parent')")))
    assert supervisor.finish(handle, timeout=10, token_usage=0)["returncode"] == 0
    assert all(row["status"] == "released" for row in queue.snapshot())


def test_managed_parent_refused_closes_its_never_bound_group_and_a_same_generation_retry_refuses_typed(tmp_path):
    supervisor, store, parent, _ready, queue = _managed_group(tmp_path)
    coordinator = supervisor.shared_resource_coordinator
    _set_limits(store, dispatch_limit=0)
    with pytest.raises(OwnershipRefused, match="DISPATCH_LIMIT_EXHAUSTED"):
        supervisor.launch(parent)
    rows = queue.snapshot()
    assert [row["status"] for row in rows] == ["released", "released"]
    group, slots, claims = _group(queue)
    assert (group["state"], group["parent_binding_json"], claims) == ("closed", None, 0)
    assert all(slot["state"] == "reserved" and slot["binding_json"] is None for slot in slots)
    assert coordinator.plan is None and coordinator.reservation is None
    # The closed envelope is terminal for this generation: a retry refuses typed before any intent.
    _set_limits(store, dispatch_limit=3)
    with pytest.raises(ResourceGroupRefused, match="RESOURCE_GROUP_NOT_AVAILABLE"):
        supervisor.launch(parent)
    assert queue.snapshot() == rows
    with store.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0


# --- R1-3: an owner killed after its intent committed, before any fork ----------------------------


def _crashing_owner(mode, boundary, root, metadata):
    """Subprocess body: build one owner, then ``os._exit`` at ``boundary`` of the armed launch."""
    root, metadata = Path(root), Path(metadata)
    root.mkdir(parents=True, exist_ok=True)
    armed = []

    def crash(point):
        if armed and point == boundary:
            os._exit(9)

    if mode == "plain":
        supervisor, store, request, _queue = _plain(root, fault=crash)
        sibling, _sibling_ready = _allocate_registered_child(store, supervisor.token, key="sibling")
        payload = {"activity_id": request.activity_id, "request_key": request.request_key, "sibling": sibling.id}
        launch = request
    else:
        supervisor, store, parent, ready, _queue = _managed_group(root)
        supervisor.fault_probe = crash
        parent = replace(parent, monitor_result=True, command=(sys.executable, "-c", _WAIT))
        payload, launch = {"workspace": str(ready.path)}, parent
        if mode == "child":
            handle = supervisor.launch(parent)
            launch = _nested(store, supervisor.token, parent, ready, "crashed-child")
            payload.update(parent_intent=handle.intent_id, activity_id=launch.activity_id)
    payload.update(db=str(store.db_path), generation=supervisor.token.generation,
                   evidence_root=str(supervisor.evidence_root), owner_pid=os.getpid())
    metadata.write_text(json.dumps(payload))
    armed.append(True)
    supervisor.launch(launch)
    os._exit(1)  # the armed fault never fired


def _crash(tmp_path, mode, boundary):
    root, metadata = tmp_path / "owner", tmp_path / "owner.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "lib") + os.pathsep + str(Path(__file__).parent)
    owner = subprocess.Popen([sys.executable, "-c",
                              "import sys, test_admission_release_before_spawn as t; t._crashing_owner(*sys.argv[1:])",
                              mode, boundary, str(root), str(metadata)], env=environment)
    try:
        assert owner.wait(timeout=180) == 9
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
    payload = json.loads(metadata.read_text())
    store = ControlStore(Path(payload["db"]))
    with store.read_transaction() as tx:
        run = tx.execute("SELECT * FROM context_runs").fetchone()
    successor = reserve_resources(store, StartRequest(
        run["run_id"], run["workspace"], run["objective_digest"], ProcessIdentity.current(),
        repository_id=run["repository_id"], planning_scope=run["planning_scope"])).token
    assert successor.generation > payload["generation"]
    queue = ManagedAdmissionQueue(root / "shared", observation_provider=_observation)
    return payload, store, successor, queue


def _crashed_intent(store, activity_id):
    with store.read_transaction() as tx:
        [intent] = tx.execute("SELECT * FROM authority_launch_intents WHERE activity_id=?", (activity_id,)).fetchall()
        marker = tx.execute("SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                            (activity_id, "spawn-attempt:" + intent["id"])).fetchone()
    assert intent["state"] == "reserved" and intent["child_pid"] is None and intent["acknowledgement_id"] is None
    return dict(intent), marker is not None


def _managed_writer(store, value="ffs-supervisor/1"):
    """Fixture: the scripted owner launched under the unmanaged writer; R1-3 proofs cover managed runs only."""
    with store.transaction() as tx:
        previous = tx.execute("SELECT writer_version FROM context_runs").fetchone()[0]
        tx.execute("UPDATE context_runs SET writer_version=?", (value,))
    return previous


@pytest.mark.parametrize(("boundary", "spawned"), [("after_intent_commit", False), ("after_spawn_before_ack", True)])
def test_plain_owner_killed_before_fork_has_its_bound_ticket_reclaimed_lazily(tmp_path, boundary, spawned):
    payload, store, successor, queue = _crash(tmp_path, "plain", boundary)
    intent, marker = _crashed_intent(store, payload["activity_id"])
    assert marker is spawned  # a fork was attempted only past the marker
    [row] = queue.snapshot()
    assert (row["status"], row["launch_intent_id"], row["child_pid"]) == ("active", intent["id"], None)
    authority = _authority(store)
    previous = _managed_writer(store)
    queue._reclaim_dead()
    [row] = queue.snapshot()
    if spawned:
        assert row["status"] == "active"  # a fork may exist: uncertain, retained
    else:
        assert (row["status"], row["limiting_resource"]) == ("reclaimed", "pre-spawn-proved")
    _managed_writer(store, previous)
    # Launch authority is unchanged: the intent and its debit stay; nothing may launch past it.
    assert _authority(store) == authority
    with store.read_transaction() as tx:
        assert tx.execute("SELECT dispatch_used FROM authority_run_limits").fetchone()[0] == 1
    with pytest.raises(OwnershipRefused, match="INTENT_RECONCILIATION_REQUIRED"):
        store.reserve_launch(payload["sibling"], successor, request_key="sibling-launch")
    replay = store.reserve_launch(payload["activity_id"], successor, request_key=payload["request_key"],
                                  request_payload=_dispatch_request(store, payload)["request"])
    assert (replay.reused, replay.id, replay.state) == (True, intent["id"], "reserved")
    assert _authority(store) == authority


def _dispatch_request(store, payload):
    with store.read_transaction() as tx:
        row = tx.execute("SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                         "WHERE k.activity_id=? AND k.idempotency_key=?",
                         (payload["activity_id"], "dispatch-request:" + payload["request_key"])).fetchone()
    return json.loads(row[0])["data"]


def _finish_resumed_parent(store, successor, payload):
    resumed = Supervisor(store, successor, evidence_root=Path(payload["evidence_root"]))
    handle = resumed.resume_monitored(payload["parent_intent"])
    (Path(payload["workspace"]) / "release").touch()
    deadline = time.monotonic() + 30
    while True:
        try:
            assert resumed.finish(handle, token_usage=0)["returncode"] == 0
            return
        except SupervisorRefused as error:
            assert error.code == "MONITOR_RESULT_PENDING" and time.monotonic() < deadline
            time.sleep(.02)


@pytest.mark.parametrize("who", ["parent", "child"])
@pytest.mark.parametrize(("boundary", "spawned"), [("after_intent_commit", False), ("after_spawn_before_ack", True)])
def test_successor_settles_a_group_whose_bound_claim_was_never_forked(tmp_path, who, boundary, spawned):
    payload, store, successor, queue = _crash(tmp_path, who, boundary)
    try:
        with store.read_transaction() as tx:
            activity = payload.get("activity_id") or tx.execute(
                "SELECT activity_id FROM authority_launch_intents").fetchone()[0]
        intent, marker = _crashed_intent(store, activity)
        assert marker is spawned
        group, _slots, claims = _group(queue)
        assert group["state"] == "reserved" and claims == int(who == "child")
        before = queue.snapshot()
        if who == "parent":
            assert json.loads(group["parent_binding_json"])["launch_intent_id"] == intent["id"]
            # A prepaid slot is settled only through its group, never by the per-ticket lazy reclaim.
            queue._reclaim_dead()
            assert queue.snapshot() == before
        else:
            _finish_resumed_parent(store, successor, payload)
        authority = _authority(store)
        _managed_writer(store)
        outcomes = settle_predecessor_groups(store, successor, queue)
        assert [item["state"] for item in outcomes] == ["retained" if spawned else "closed"], outcomes
        rows = queue.snapshot()
        if spawned:
            assert [row["status"] for row in rows] == [row["status"] for row in before]
        else:
            assert all(row["status"] == "released" for row in rows), rows
        assert _authority(store) == authority  # the never-forked intent and its debit stay as they were
    finally:
        (Path(payload["workspace"]) / "release").touch()
