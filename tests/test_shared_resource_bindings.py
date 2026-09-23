"""Read-only lease reconciliation binds historical authority, not dead PID alone."""
from dataclasses import replace
import time

from process_identity import ProcessIdentity
from run_state.managed_admission import LeaseIdentity, ManagedAdmissionQueue
from run_state.ownership import release_owner
from run_state.shared_resources import ControlStoreLeaseEvidenceReader, record_admission_request
from run_state.shared_resources import SharedResourceCoordinator
from run_state.resource_observation import ResourceDemand, ResourceObservation
from test_runtime_receipt_authority import _managed_store


def test_prespawn_proof_requires_retired_exact_request_and_dead_writer(tmp_path, monkeypatch):
    store, token, _workspace = _managed_store(tmp_path.resolve())
    request = record_admission_request(store, token, activity_id="activity", request_key="dispatch",
                                       material={"command_sha256": "a" * 64})
    assert record_admission_request(store, token, activity_id="activity", request_key="dispatch",
                                    material={"command_sha256": "a" * 64}) == request
    identity = LeaseIdentity(token.repository_id, token.run_id, request["request_key"], None,
                             token.generation, ProcessIdentity.current())
    reader = ControlStoreLeaseEvidenceReader(store.db_path)
    monkeypatch.setattr("run_state.shared_resources.probe_identity", lambda _identity: "DEAD")
    assert reader.read_fenced_lease(identity) is None  # still held despite dead probe
    with store.transaction() as tx:
        release_owner(tx, token)
    proof = reader.read_fenced_lease(identity)
    assert proof is not None and proof.proves_never_authorized(identity)
    for forged in (replace(identity, request_key="other"), replace(identity, generation=token.generation + 1),
                   replace(identity, run_id="other"), replace(identity, launch_intent_id="unknown-intent")):
        assert reader.read_fenced_lease(forged) is None
    monkeypatch.setattr("run_state.shared_resources.probe_identity", lambda _identity: "UNKNOWN")
    assert reader.read_fenced_lease(identity) is None


def test_existing_dispatch_event_prevents_absence_proof(tmp_path, monkeypatch):
    store, token, _workspace = _managed_store(tmp_path.resolve())
    request = record_admission_request(store, token, activity_id="activity", request_key="dispatch",
                                       material={"command_sha256": "a" * 64})
    store.record_event_once(token, "activity", "dispatch-request:dispatch", {"intent_id": "retained"})
    identity = LeaseIdentity(token.repository_id, token.run_id, request["request_key"], None,
                             token.generation, ProcessIdentity.current())
    with store.transaction() as tx:
        release_owner(tx, token)
    monkeypatch.setattr("run_state.shared_resources.probe_identity", lambda _identity: "DEAD")
    assert ControlStoreLeaseEvidenceReader(store.db_path).read_fenced_lease(identity) is None


def test_registry_reconciles_unmatched_lease_from_read_only_retired_authority(tmp_path, monkeypatch):
    store, token, _workspace = _managed_store(tmp_path.resolve())
    request = record_admission_request(store, token, activity_id="activity", request_key="dispatch",
                                       material={"command_sha256": "a" * 64})
    queue = ManagedAdmissionQueue(tmp_path.resolve() / "shared-registry")
    ticket = queue.enqueue(state_root=store.db_path.parent, repository_id=token.repository_id,
                           run_id=token.run_id, request_key=request["request_key"], generation=token.generation)
    monkeypatch.setattr("run_state.shared_resources.probe_identity", lambda _identity: "DEAD")
    monkeypatch.setattr("run_state.managed_admission.probe_identity", lambda _identity: "DEAD")
    queue._reclaim_dead()
    assert queue.status(ticket)["status"] == "waiting"
    with store.transaction() as tx:
        release_owner(tx, token)
    queue._reclaim_dead()
    assert queue.status(ticket)["status"] == "reclaimed"
    assert queue.status(ticket)["limiting_resource"] == "pre-spawn-proved"
    queue._reclaim_dead()
    assert len(queue.snapshot()) == 1


def test_coordinator_waits_then_admits_group_without_charging_launches(tmp_path):
    store, token, _workspace = _managed_store(tmp_path.resolve())
    samples = []

    def observe():
        samples.append(1)
        return ResourceObservation(time.monotonic_ns(), 0 if len(samples) == 1 else 2,
                                   1 << 30, 1 << 30, 100, 100, {})

    queue = ManagedAdmissionQueue(tmp_path.resolve() / "shared-registry", observation_provider=observe)
    coordinator = SharedResourceCoordinator(store, token, queue=queue, poll_seconds=.001)
    requests = tuple({"activity_id": "activity", "request_key": key,
                      "material": {"command_sha256": key * 64}, "demand": ResourceDemand(cpu=1)}
                     for key in ("a", "b"))
    reservations = coordinator.acquire(requests, group_key="required-overlap")
    assert len(samples) >= 2
    assert len(reservations) == 2
    assert all(queue.status(ticket)["status"] == "active" for ticket, _binding in reservations)
    assert coordinator.acquire(requests, group_key="required-overlap") == reservations
    assert len(queue.snapshot()) == 2
    with store.read_transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM authority_launch_intents").fetchone()[0] == 0
    coordinator.bind_intent(reservations[0], "exact-intent")
    assert queue.status(reservations[0][0])["launch_intent_id"] == "exact-intent"
