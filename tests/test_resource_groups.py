from __future__ import annotations

import hashlib
import json
import time

import pytest

from process_identity import ProcessIdentity
from run_state import managed_admission
from run_state.managed_admission import ManagedAdmissionQueue
from run_state.managed_admission import LeaseIdentity
from run_state.resource_groups import (
    GroupMember, GroupPlan, LaunchBinding, ResourceGroupRefused, ResourceParentGroupRegistry,
)
from run_state.resource_observation import ResourceDemand, ResourceObservation


OWNER = ProcessIdentity("host", "boot", 7, "start")
CHILD = ProcessIdentity("host", "boot", 8, "child")


class Evidence:
    def __init__(self, dead=(), never_authorized=True):
        self.dead = set(dead)
        self.never_authorized = never_authorized

    def native_state(self, identity):
        return "DEAD" if identity in self.dead else "LIVE"

    def proves_never_authorized(self, _lease):
        return self.never_authorized


def queue(tmp_path, monkeypatch, cpu=4):
    monkeypatch.setattr(managed_admission.ProcessIdentity, "current", staticmethod(lambda: OWNER))
    monkeypatch.setattr(managed_admission, "probe_identity", lambda _: "LIVE")
    return ManagedAdmissionQueue(tmp_path.resolve() / "admission", observation_provider=lambda: ResourceObservation(
        time.monotonic_ns(), cpu, 1 << 30, 1 << 30, 10, 100, {"codex": 4}))


def plan(tmp_path, *, expires=None):
    members = (GroupMember("parent", "parent", ResourceDemand(cpu=1, provider="codex", provider_units=1)),
               GroupMember("child-a", "child", ResourceDemand(cpu=1, provider="codex", provider_units=1)),
               GroupMember("child-b", "child", ResourceDemand(cpu=1, provider="codex", provider_units=1)))
    raw = {"schema": "ffs.resource-parent-group/v1", "group_id": "g", "repository_id": "repo", "run_id": "run",
           "generation": 2, "state_root": str(tmp_path.resolve()), "parent_request_key": "parent-request",
           "members": [member.record() for member in members]}
    return GroupPlan("g", "repo", "run", 2, str(tmp_path.resolve()), "parent-request", members,
                     hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                     time.monotonic_ns() + 10_000_000_000 if expires is None else expires)


def binding(key="child-request", intent="intent", demand_owner=CHILD):
    return LaunchBinding("repo", "run", key, intent, 2, OWNER, demand_owner)


def test_parent_and_child_envelope_is_prepaid_without_double_count(tmp_path, monkeypatch):
    registry = ResourceParentGroupRegistry(queue(tmp_path, monkeypatch), Evidence(dead={CHILD}))
    current = plan(tmp_path)
    reservation = registry.reserve(current)
    assert reservation.state == "reserved"
    assert len(registry.queue.snapshot()) == 3
    registry.claim_parent(current, binding("parent-request", "parent-intent", OWNER))
    first = registry.claim_child(current, binding("one", "one-intent"), ResourceDemand(cpu=1, provider="codex", provider_units=1))
    assert len(registry.queue.snapshot()) == 3
    registry.finish_child(current, first)
    second = registry.claim_child(current, binding("two", "two-intent"), ResourceDemand(cpu=1, provider="codex", provider_units=1))
    assert second.slot_id == first.slot_id
    assert len(registry.queue.snapshot()) == 3


def test_preserved_pre_spawn_claim_requires_exact_native_ack_before_release(tmp_path, monkeypatch):
    evidence = Evidence()
    registry = ResourceParentGroupRegistry(queue(tmp_path, monkeypatch), evidence)
    current = plan(tmp_path)
    registry.reserve(current)
    registry.hold_parent_launch(current)
    assert not registry.expire_staging(current, now_ns=current.staging_expires_ns + 1)
    registry.claim_parent(current, binding('parent-request', 'parent-intent', None))
    registry.claim_parent(current, binding('parent-request', 'parent-intent', OWNER))
    demand = ResourceDemand(cpu=1, provider='codex', provider_units=1)
    claim = registry.claim_child(current, binding('child', 'child-intent', None), demand)
    with pytest.raises(ResourceGroupRefused, match='CHILD_RETAINED'):
        registry.finish_child(current, claim)
    with pytest.raises(ResourceGroupRefused, match='BINDING_MISMATCH'):
        registry.claim_child(current, claim.binding, ResourceDemand(cpu=0, provider='codex', provider_units=1))
    acknowledged = registry.bind_child_consumer(current, claim, CHILD)
    assert registry.bind_child_consumer(current, claim, CHILD) == acknowledged
    with pytest.raises(ResourceGroupRefused, match='CHILD_RETAINED'):
        registry.finish_child(current, acknowledged)
    evidence.dead.add(CHILD)
    registry.finish_child(current, acknowledged)


def test_release_unissued_hold_refuses_live_reserving_owner(tmp_path, monkeypatch):
    """Fixture native identity/probe and resource observation; no host qualification."""
    evidence = Evidence()
    registry = ResourceParentGroupRegistry(queue(tmp_path, monkeypatch), evidence)
    current = plan(tmp_path)
    assert registry.reserve(current).state == "reserved"
    registry.hold_parent_launch(current)
    before = registry.status(current.group_id)
    admissions = registry.queue.snapshot()
    assert evidence.native_state(OWNER) == "LIVE"
    assert before["group"]["parent_binding_json"] is None
    assert all(slot["binding_json"] is None for slot in before["slots"])
    assert next(slot for slot in before["slots"] if slot["role"] == "parent")["ever_launched"] == 1
    assert len(admissions) == len(current.members)
    assert all(row["status"] == "active" for row in admissions)

    with pytest.raises(ResourceGroupRefused) as refused:
        registry.release_unissued_hold(current, "a" * 64)
    assert refused.value.code == "RESOURCE_GROUP_MEMBER_RETAINED"
    assert registry.status(current.group_id) == before
    assert registry.queue.snapshot() == admissions

    # Only the fixture native probe changes: the same valid proof can now close the hold.
    evidence.dead.add(OWNER)
    registry.release_unissued_hold(current, "a" * 64)
    after = registry.status(current.group_id)
    assert after["group"]["state"] == "closed"
    assert after["group"]["parent_end_proof_sha256"] == "a" * 64
    released = registry.queue.snapshot()
    assert [(row["sequence"], row["ticket"]) for row in released] == [
        (row["sequence"], row["ticket"]) for row in admissions]
    assert all(row["status"] == "released" for row in released)


def test_child_slots_reject_wrong_binding_and_oversized_or_wrong_provider_demand(tmp_path, monkeypatch):
    registry = ResourceParentGroupRegistry(queue(tmp_path, monkeypatch), Evidence(dead={CHILD}))
    current = plan(tmp_path)
    registry.reserve(current)
    with pytest.raises(ResourceGroupRefused, match="BINDING_MISMATCH"):
        registry.claim_child(current, LaunchBinding("repo", "other", "x", "i", 2, OWNER, CHILD), ResourceDemand(cpu=1))
    with pytest.raises(ResourceGroupRefused, match="SLOT_UNAVAILABLE"):
        registry.claim_child(current, binding(), ResourceDemand(cpu=2, provider="codex", provider_units=1))
    with pytest.raises(ResourceGroupRefused, match="SLOT_UNAVAILABLE"):
        registry.claim_child(current, binding(), ResourceDemand(cpu=1, provider="claude", provider_units=1))
    claim = registry.claim_child(current, binding("bound", "bound-intent"), ResourceDemand(cpu=1, provider="codex", provider_units=1))
    with pytest.raises(ResourceGroupRefused, match="BINDING_MISMATCH"):
        registry.claim_child(current, binding("bound", "different-intent"), ResourceDemand(cpu=1, provider="codex", provider_units=1))
    registry.finish_child(current, claim)


def test_target_contraction_does_not_revoke_unstarted_live_parent_slots(tmp_path, monkeypatch):
    registry = ResourceParentGroupRegistry(queue(tmp_path, monkeypatch), Evidence())
    current = plan(tmp_path)
    registry.reserve(current)
    registry.claim_parent(current, binding("parent-request", "parent-intent", OWNER))
    # No target argument appears in this API: ordinary scheduler contraction is
    # intentionally unable to mutate an admitted group's prepaid envelope.
    state = registry.status("g")
    assert state["group"]["state"] == "reserved"
    assert {slot["state"] for slot in state["slots"] if slot["role"] == "child"} == {"reserved"}
    assert all(row["status"] == "active" for row in registry.queue.snapshot())


def test_parent_death_retains_live_child_until_proven_settlement(tmp_path, monkeypatch):
    registry = ResourceParentGroupRegistry(queue(tmp_path, monkeypatch), Evidence(dead={OWNER, CHILD}))
    current = plan(tmp_path)
    registry.reserve(current)
    parent = binding("parent-request", "parent-intent", OWNER)
    registry.claim_parent(current, parent)
    claim = registry.claim_child(current, binding("child", "child-intent"), ResourceDemand(cpu=1, provider="codex", provider_units=1))
    registry.mark_parent_ended(current, parent, "a" * 64)
    with pytest.raises(ResourceGroupRefused, match="CHILD_RETAINED"):
        registry.close_after_parent_end(current)
    assert registry.status("g")["group"]["state"] == "parent_ended"
    registry.finish_child(current, claim)
    registry.close_after_parent_end(current)
    assert {row["status"] for row in registry.queue.snapshot()} == {"released"}


def test_prelaunch_staging_expiry_releases_only_unconsumed_envelope(tmp_path, monkeypatch):
    registry = ResourceParentGroupRegistry(queue(tmp_path, monkeypatch, cpu=0), Evidence())
    current = plan(tmp_path, expires=time.monotonic_ns() - 1)
    reservation = registry.reserve(current)
    assert reservation.state == "staging"
    assert registry.expire_staging(current)
    assert registry.status("g")["group"]["state"] == "expired"
    assert {row["status"] for row in registry.queue.snapshot()} == {"released"}
    oldest_age = registry.status("g")["group"]["age_ns"]
    registry.queue._observe = lambda: ResourceObservation(time.monotonic_ns(), 4, 1 << 30, 1 << 30, 10, 100, {"codex": 4})
    retried = registry.retry_expired_staging(current)
    assert retried.state == "reserved"
    assert {row["group_age_ns"] for row in registry.queue.snapshot()} == {oldest_age}


def test_staging_group_retries_measured_admission_when_capacity_recovers(tmp_path, monkeypatch):
    queue_instance = queue(tmp_path, monkeypatch, cpu=0)
    registry = ResourceParentGroupRegistry(queue_instance, Evidence())
    current = plan(tmp_path)
    assert registry.reserve(current).state == "staging"
    queue_instance._observe = lambda: ResourceObservation(time.monotonic_ns(), 4, 1 << 30, 1 << 30, 10, 100, {"codex": 4})
    assert registry.reserve(current).state == "reserved"


def test_staging_resume_repairs_a_crashed_partial_slot_record_before_admission(tmp_path, monkeypatch):
    queue_instance = queue(tmp_path, monkeypatch, cpu=0)
    registry = ResourceParentGroupRegistry(queue_instance, Evidence())
    current = plan(tmp_path)
    assert registry.reserve(current).state == "staging"
    with queue_instance._transaction() as connection:
        connection.execute("DELETE FROM resource_parent_group_slots WHERE group_id='g' AND slot_id='child-b'")
    queue_instance._observe = lambda: ResourceObservation(time.monotonic_ns(), 4, 1 << 30, 1 << 30, 10, 100, {"codex": 4})
    assert registry.reserve(current).state == "reserved"
    assert len(registry.status("g")["slots"]) == 3


def test_forged_parent_hash_and_bind_before_slot_record_never_refund_capacity(tmp_path, monkeypatch):
    registry = ResourceParentGroupRegistry(queue(tmp_path, monkeypatch), Evidence())
    current = plan(tmp_path, expires=time.monotonic_ns() - 1)
    reservation = registry.reserve(current)
    parent_ticket = reservation.tickets["parent"]
    # Simulate the crash boundary after queue bind and before the component
    # records `ever_launched`: expiry must inspect managed_admissions too.
    registry.queue.bind_consumer(parent_ticket, LeaseIdentity("repo", "run", "parent-request", "intent", 2, OWNER, OWNER))
    with pytest.raises(ResourceGroupRefused, match="MEMBER_RETAINED"):
        registry.expire_staging(current)

    healthy = ResourceParentGroupRegistry(queue(tmp_path / "proof", monkeypatch), Evidence())
    fresh = plan(tmp_path / "proof")
    healthy.reserve(fresh)
    healthy.claim_parent(fresh, binding("parent-request", "parent-intent", OWNER))
    with pytest.raises(ResourceGroupRefused, match="PARENT_RETAINED"):
        healthy.mark_parent_ended(fresh, binding("parent-request", "parent-intent", OWNER), "f" * 64)


def test_legacy_dispatch_and_parent_group_share_one_capacity_authority(tmp_path, monkeypatch):
    """Fixture identity/probe and resource observation; no host qualification."""
    shared = queue(tmp_path, monkeypatch, cpu=4)

    # The pre-parent-group path reserves one ordinary per-dispatch ticket.
    legacy = shared.enqueue(
        state_root=tmp_path.resolve(),
        repository_id="legacy-repo",
        run_id="legacy-run",
        request_key="legacy-parent",
        generation=1,
        demand=ResourceDemand(cpu=1, provider="codex", provider_units=1),
    )
    assert shared.try_admit(legacy)
    shared.bind_consumer(
        legacy,
        managed_admission.LeaseIdentity(
            "legacy-repo", "legacy-run", "legacy-parent", "legacy-intent", 1, OWNER, CHILD
        ),
    )

    # The prepaid parent envelope uses the same queue and consumes the other
    # three CPU units; it does not create a second capacity authority.
    registry = ResourceParentGroupRegistry(shared, Evidence())
    current = plan(tmp_path)
    assert registry.reserve(current).state == "reserved"
    group_before = registry.status(current.group_id)
    rows = shared.snapshot()
    assert len(rows) == 4
    assert all(row["status"] == "active" for row in rows)
    assert next(row for row in rows if row["ticket"] == legacy.ticket)["group_id"] is None
    assert {row["group_id"] for row in rows if row["ticket"] != legacy.ticket} == {current.group_id}

    waiting = shared.enqueue(
        state_root=(tmp_path / "waiting").resolve(),
        repository_id="waiting-repo",
        run_id="waiting-run",
        request_key="waiting-parent",
        generation=1,
        demand=ResourceDemand(cpu=1, provider="codex", provider_units=1),
    )
    assert not shared.try_admit(waiting)
    assert shared.status(waiting)["limiting_resource"] == "cpu"

    # Release still requires the ordinary ticket's bound consumer to be
    # natively dead. Releasing it admits the waiter without changing any
    # prepaid group record or ticket.
    monkeypatch.setattr(
        managed_admission,
        "probe_identity",
        lambda identity: "DEAD" if identity == CHILD else "LIVE",
    )
    shared.release(legacy)
    assert registry.status(current.group_id) == group_before
    group_rows = [row for row in shared.snapshot() if row["group_id"] == current.group_id]
    assert group_rows == [row for row in rows if row["group_id"] == current.group_id]
    assert len(group_rows) == 3
    assert all(row["status"] == "active" for row in group_rows)
    assert shared.try_admit(waiting)
    assert shared.status(waiting)["status"] == "active"
    assert shared.status(legacy)["status"] == "released"
