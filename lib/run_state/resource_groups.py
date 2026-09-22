"""Prepaid parent/child resource groups in the existing admission registry.

The caller supplies a frozen, hash-bound member inventory and does all
ControlStore/native-process proof outside this module.  This registry component
only preserves the prepaid shared-resource envelope and exact slot bindings.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import time
from typing import Mapping, Protocol

from process_identity import ProcessIdentity

from .managed_admission import AdmissionTicket, LeaseIdentity, ManagedAdmissionQueue, ManagedAdmissionRefused
from .resource_observation import ResourceDemand


_SCHEMA = "ffs.resource-parent-group/v1"
_HEX = set("0123456789abcdef")


class ResourceGroupRefused(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class GroupEvidenceReader(Protocol):
    """Read-only liveness/authority proof seam; never called under SQL."""

    def native_state(self, identity: ProcessIdentity) -> str: ...

    def proves_never_authorized(self, lease: LeaseIdentity) -> bool: ...


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _sha(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _identity(value: ProcessIdentity) -> dict:
    return asdict(value)


def _parse_identity(value) -> ProcessIdentity:
    if not isinstance(value, dict) or set(value) != {"host_id", "boot_id", "pid", "start_token"}:
        raise ResourceGroupRefused("RESOURCE_GROUP_RECORD_INVALID")
    try:
        return ProcessIdentity(**value)
    except (TypeError, ValueError) as error:
        raise ResourceGroupRefused("RESOURCE_GROUP_RECORD_INVALID") from error


@dataclass(frozen=True)
class GroupMember:
    slot_id: str
    role: str  # exactly one parent and zero or more reusable child envelopes
    demand: ResourceDemand

    def __post_init__(self):
        if (not isinstance(self.slot_id, str) or not self.slot_id or ":" in self.slot_id
                or self.role not in {"parent", "child"} or not isinstance(self.demand, ResourceDemand)):
            raise ResourceGroupRefused("RESOURCE_GROUP_INVENTORY_INVALID")

    def record(self) -> dict:
        return {"slot_id": self.slot_id, "role": self.role, "demand": self.demand.record()}


@dataclass(frozen=True)
class GroupPlan:
    group_id: str
    repository_id: str
    run_id: str
    generation: int
    state_root: str
    parent_request_key: str
    members: tuple[GroupMember, ...]
    inventory_sha256: str
    staging_expires_ns: int
    plan_inventory_sha256: str | None = None

    def __post_init__(self):
        if (not all(isinstance(value, str) and value for value in
                    (self.group_id, self.repository_id, self.run_id, self.state_root, self.parent_request_key))
                or type(self.generation) is not int or self.generation <= 0
                or type(self.staging_expires_ns) is not int or self.staging_expires_ns <= 0
                or not _sha(self.inventory_sha256) or not self.members
                or (self.plan_inventory_sha256 is not None and not _sha(self.plan_inventory_sha256))
                or sum(member.role == "parent" for member in self.members) != 1
                or len({member.slot_id for member in self.members}) != len(self.members)):
            raise ResourceGroupRefused("RESOURCE_GROUP_PLAN_INVALID")
        if self.inventory_sha256 != _hash(self.inventory_record()):
            raise ResourceGroupRefused("RESOURCE_GROUP_INVENTORY_HASH_INVALID")

    def inventory_record(self) -> dict:
        material = {
            "schema": _SCHEMA, "group_id": self.group_id, "repository_id": self.repository_id,
            "run_id": self.run_id, "generation": self.generation, "state_root": self.state_root,
            "parent_request_key": self.parent_request_key,
            "members": [member.record() for member in self.members],
        }
        if self.plan_inventory_sha256 is not None:
            material['plan_inventory_sha256'] = self.plan_inventory_sha256
        return material


@dataclass(frozen=True)
class LaunchBinding:
    repository_id: str
    run_id: str
    request_key: str
    launch_intent_id: str
    generation: int
    supervisor: ProcessIdentity
    consumer: ProcessIdentity | None

    def __post_init__(self):
        if (not all(isinstance(value, str) and value for value in
                    (self.repository_id, self.run_id, self.request_key, self.launch_intent_id))
                or type(self.generation) is not int or self.generation <= 0
                or not isinstance(self.supervisor, ProcessIdentity)
                or (self.consumer is not None and not isinstance(self.consumer, ProcessIdentity))):
            raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_INVALID")

    def record(self) -> dict:
        return {"repository_id": self.repository_id, "run_id": self.run_id,
                "request_key": self.request_key, "launch_intent_id": self.launch_intent_id,
                "generation": self.generation, "supervisor": _identity(self.supervisor),
                "consumer": None if self.consumer is None else _identity(self.consumer)}


@dataclass(frozen=True)
class GroupReservation:
    group_id: str
    tickets: Mapping[str, AdmissionTicket]
    state: str


@dataclass(frozen=True)
class ChildClaim:
    group_id: str
    slot_id: str
    binding: LaunchBinding
    demand: ResourceDemand


_DDL = (
"""CREATE TABLE IF NOT EXISTS resource_parent_groups (
 group_id TEXT PRIMARY KEY, plan_json TEXT NOT NULL, inventory_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('staging','reserved','parent_ended','closed','expired')),
 age_ns INTEGER NOT NULL, expires_ns INTEGER NOT NULL, parent_binding_json TEXT,
 parent_end_proof_sha256 TEXT
);""",
"""CREATE TABLE IF NOT EXISTS resource_parent_group_slots (
 group_id TEXT NOT NULL, slot_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('parent','child')),
 ticket_sequence INTEGER NOT NULL, ticket TEXT NOT NULL, owner_json TEXT NOT NULL,
 demand_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('reserved','claimed')),
 binding_json TEXT, ever_launched INTEGER NOT NULL DEFAULT 0 CHECK(ever_launched IN (0,1)),
 PRIMARY KEY(group_id,slot_id), UNIQUE(ticket_sequence,ticket)
);""",
"""CREATE UNIQUE INDEX IF NOT EXISTS resource_parent_group_child_binding
 ON resource_parent_group_slots(group_id,binding_json) WHERE binding_json IS NOT NULL;""",
"""CREATE TABLE IF NOT EXISTS resource_parent_group_claims (
 group_id TEXT NOT NULL, request_key TEXT NOT NULL, binding_json TEXT NOT NULL,
 demand_json TEXT NOT NULL, slot_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','finished')),
 PRIMARY KEY(group_id,request_key)
);""",
)


class ResourceParentGroupRegistry:
    """Registry-only lifecycle for an already trusted, frozen parent group.

    ``reserve`` calls the queue's measured group admission.  It never samples
    resources while holding this component's SQL transaction.  Callers must
    recheck physical safety immediately before spawn and separately prove
    parent end/intent binding before requesting close.
    """

    def __init__(self, queue: ManagedAdmissionQueue, evidence_reader: GroupEvidenceReader):
        if not isinstance(queue, ManagedAdmissionQueue):
            raise TypeError("queue must be ManagedAdmissionQueue")
        if not all(callable(getattr(evidence_reader, name, None)) for name in ("native_state", "proves_never_authorized")):
            raise TypeError("evidence_reader must provide read-only proof methods")
        self.queue, self.evidence_reader = queue, evidence_reader
        with queue._transaction() as connection:
            for statement in _DDL:
                connection.execute(statement)

    def reserve(self, plan: GroupPlan) -> GroupReservation:
        """Admit every prepaid ticket or leave all members non-startable."""
        self._ensure_plan(plan)
        existing = self._reservation(plan)
        if existing is not None:
            if existing.state == "expired":
                raise ResourceGroupRefused("RESOURCE_GROUP_STAGING_EXPIRED")
            if existing.state == "reserved":
                with self.queue._connection() as connection:
                    active = connection.execute(
                        "SELECT COUNT(*) FROM managed_admissions m JOIN resource_parent_group_slots s "
                        "ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket WHERE s.group_id=? AND m.status='active'",
                        (plan.group_id,),
                    ).fetchone()[0]
                if active != len(plan.members):
                    raise ResourceGroupRefused("RESOURCE_GROUP_ADMISSION_UNCERTAIN")
                return existing
            if existing.state != "staging":
                raise ResourceGroupRefused("RESOURCE_GROUP_NOT_AVAILABLE")
        else:
            now = time.monotonic_ns()
            with self.queue._transaction() as connection:
                connection.execute(
                    "INSERT INTO resource_parent_groups(group_id,plan_json,inventory_sha256,state,age_ns,expires_ns) "
                    "VALUES(?,?,?,'staging',?,?)", (plan.group_id, _canonical(plan.inventory_record()),
                                                       plan.inventory_sha256, now, plan.staging_expires_ns),
                )
        tickets: dict[str, AdmissionTicket] = {}
        try:
            for member in plan.members:
                tickets[member.slot_id] = self.queue.enqueue(
                    state_root=plan.state_root, repository_id=plan.repository_id, run_id=plan.run_id,
                    request_key=plan.parent_request_key if member.role == "parent"
                    else "parent-group:" + plan.group_id + ":" + member.slot_id,
                    generation=plan.generation, demand=member.demand, group_id=plan.group_id,
                    group_width=len(plan.members),
                )
            with self.queue._transaction() as connection:
                for member in plan.members:
                    ticket = tickets[member.slot_id]
                    connection.execute(
                        "INSERT INTO resource_parent_group_slots(group_id,slot_id,role,ticket_sequence,ticket,owner_json,demand_json,state) "
                        "VALUES(?,?,?,?,?,?,?,'reserved') ON CONFLICT(group_id,slot_id) DO NOTHING", (plan.group_id, member.slot_id, member.role,
                         ticket.sequence, ticket.ticket, _canonical(_identity(ticket.owner)), _canonical(member.demand.record())),
                    )
        except BaseException:
            # No member reached a bind/claim; only these freshly staged tickets
            # may be released.  A process crash remains conservatively visible.
            for ticket in tickets.values():
                try:
                    self.queue.release(ticket)
                except ManagedAdmissionRefused:
                    pass
            raise
        parent = next(member for member in plan.members if member.role == "parent")
        if not self.queue.try_admit(tickets[parent.slot_id]):
            return GroupReservation(plan.group_id, tickets, "staging")
        with self.queue._transaction() as connection:
            rows = connection.execute("SELECT slot_id,ticket_sequence,ticket FROM resource_parent_group_slots WHERE group_id=?",
                                      (plan.group_id,)).fetchall()
            if len(rows) != len(plan.members) or any(
                    row["slot_id"] not in tickets or tickets[row["slot_id"]].sequence != row["ticket_sequence"]
                    or tickets[row["slot_id"]].ticket != row["ticket"] for row in rows):
                raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            active = connection.execute(
                "SELECT COUNT(*) FROM managed_admissions m JOIN resource_parent_group_slots s "
                "ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket WHERE s.group_id=? AND m.status='active'",
                (plan.group_id,),
            ).fetchone()[0]
            if active != len(plan.members):
                raise ResourceGroupRefused("RESOURCE_GROUP_ADMISSION_UNCERTAIN")
            connection.execute("UPDATE resource_parent_groups SET state='reserved' WHERE group_id=? AND state='staging'",
                               (plan.group_id,))
        return GroupReservation(plan.group_id, tickets, "reserved")

    def hold_parent_launch(self, plan: GroupPlan) -> None:
        """Fence staging expiry before crossing into ControlStore intent creation.

        A crash after this point retains the envelope until authority proves
        whether a launch was issued. Elapsed staging time is no longer proof.
        """
        owner = self.queue._owner()
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            group = connection.execute("SELECT state FROM resource_parent_groups WHERE group_id=?",
                                       (plan.group_id,)).fetchone()
            active = connection.execute("SELECT COUNT(*) FROM managed_admissions m JOIN resource_parent_group_slots s "
                "ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket WHERE s.group_id=? AND m.status='active'",
                (plan.group_id,)).fetchone()[0]
            if group is None or group['state'] != 'reserved' or active != len(plan.members):
                raise ResourceGroupRefused("RESOURCE_GROUP_NOT_AVAILABLE")
            rows = connection.execute('SELECT owner_json FROM resource_parent_group_slots WHERE group_id=?',
                                      (plan.group_id,)).fetchall()
            if any(_parse_identity(json.loads(row['owner_json'])) != owner for row in rows):
                raise ResourceGroupRefused('RESOURCE_GROUP_BINDING_MISMATCH')
            connection.execute("UPDATE resource_parent_group_slots SET ever_launched=1 WHERE group_id=? AND role='parent'",
                               (plan.group_id,))

    def claim_parent(self, plan: GroupPlan, binding: LaunchBinding) -> AdmissionTicket:
        self._ensure_binding(plan, binding, parent=True)
        with self.queue._connection() as connection:
            self._stored_plan(connection, plan)
            state = connection.execute("SELECT state FROM resource_parent_groups WHERE group_id=?", (plan.group_id,)).fetchone()
            active = connection.execute(
                "SELECT COUNT(*) FROM managed_admissions m JOIN resource_parent_group_slots s "
                "ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket WHERE s.group_id=? AND m.status='active'",
                (plan.group_id,),
            ).fetchone()[0]
            row = connection.execute("SELECT * FROM resource_parent_group_slots WHERE group_id=? AND slot_id='parent'",
                                     (plan.group_id,)).fetchone()
        if state is None or state["state"] != "reserved" or active != len(plan.members) or row is None:
            raise ResourceGroupRefused("RESOURCE_GROUP_NOT_AVAILABLE")
        if row["state"] == "claimed":
            if row["binding_json"] == _canonical(binding.record()):
                return self._ticket(row)
            from dataclasses import replace
            previous = self._binding(row['binding_json'])
            if (previous.consumer is None and binding.consumer is not None
                    and replace(previous, consumer=binding.consumer) == binding):
                ticket = self._ticket(row)
                self.queue.bind_consumer(ticket, LeaseIdentity(binding.repository_id, binding.run_id,
                    binding.request_key, binding.launch_intent_id, binding.generation,
                    binding.supervisor, binding.consumer))
                with self.queue._transaction() as connection:
                    self._stored_plan(connection, plan)
                    changed = connection.execute("UPDATE resource_parent_group_slots SET binding_json=? "
                        "WHERE group_id=? AND slot_id='parent' AND state='claimed' AND binding_json=?",
                        (_canonical(binding.record()), plan.group_id, row['binding_json'])).rowcount
                    if changed != 1:
                        raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
                    connection.execute("UPDATE resource_parent_groups SET parent_binding_json=? WHERE group_id=?",
                                       (_canonical(binding.record()), plan.group_id))
                return ticket
            raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_MISMATCH")
        ticket = self._ticket(row)
        if ticket.owner != binding.supervisor:
            raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_MISMATCH")
        self.queue.bind_consumer(ticket, LeaseIdentity(binding.repository_id, binding.run_id, binding.request_key,
                                                        binding.launch_intent_id, binding.generation,
                                                        binding.supervisor, binding.consumer))
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            active = connection.execute(
                "SELECT COUNT(*) FROM managed_admissions m JOIN resource_parent_group_slots s "
                "ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket WHERE s.group_id=? AND m.status='active'",
                (plan.group_id,),
            ).fetchone()[0]
            if active != len(plan.members):
                raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            changed = connection.execute(
                "UPDATE resource_parent_group_slots SET state='claimed',binding_json=?,ever_launched=1 "
                "WHERE group_id=? AND slot_id=? AND state='reserved' AND binding_json IS NULL",
                (_canonical(binding.record()), plan.group_id, row["slot_id"]),
            ).rowcount
            if changed != 1:
                raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            connection.execute("UPDATE resource_parent_groups SET parent_binding_json=? WHERE group_id=? AND state='reserved'",
                               (_canonical(binding.record()), plan.group_id))
        return ticket

    def claim_child(self, plan: GroupPlan, binding: LaunchBinding, demand: ResourceDemand) -> ChildClaim:
        self._ensure_binding(plan, binding, parent=False)
        if not isinstance(demand, ResourceDemand):
            raise ResourceGroupRefused("RESOURCE_GROUP_DEMAND_INVALID")
        encoded = _canonical(binding.record())
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            group = connection.execute("SELECT state FROM resource_parent_groups WHERE group_id=?", (plan.group_id,)).fetchone()
            if group is None or group["state"] != "reserved":
                raise ResourceGroupRefused("RESOURCE_GROUP_NOT_AVAILABLE")
            prior = connection.execute("SELECT * FROM resource_parent_group_claims WHERE group_id=? AND request_key=?",
                                       (plan.group_id, binding.request_key)).fetchone()
            if prior is not None:
                if (prior["state"] == "active" and prior["binding_json"] == encoded
                        and prior["demand_json"] == _canonical(demand.record())):
                    return ChildClaim(plan.group_id, prior["slot_id"], binding, demand)
                raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_MISMATCH")
            slots = connection.execute("SELECT * FROM resource_parent_group_slots WHERE group_id=? AND role='child' "
                                       "AND state='reserved' ORDER BY slot_id", (plan.group_id,)).fetchall()
            for slot in slots:
                if self._parse_slot_owner(slot) != binding.supervisor:
                    raise ResourceGroupRefused('RESOURCE_GROUP_BINDING_MISMATCH')
                capacity = ResourceDemand(**json.loads(slot["demand_json"]))
                if not self._compatible(demand, capacity):
                    continue
                changed = connection.execute(
                    "UPDATE resource_parent_group_slots SET state='claimed',binding_json=?,ever_launched=1 "
                    "WHERE group_id=? AND slot_id=? AND state='reserved' AND binding_json IS NULL",
                    (encoded, plan.group_id, slot["slot_id"]),
                ).rowcount
                if changed == 1:
                    connection.execute(
                        "INSERT INTO resource_parent_group_claims(group_id,request_key,binding_json,demand_json,slot_id,state) "
                        "VALUES(?,?,?,?,?,'active')", (plan.group_id, binding.request_key, encoded,
                                                        _canonical(demand.record()), slot["slot_id"]),
                    )
                    return ChildClaim(plan.group_id, slot["slot_id"], binding, demand)
            raise ResourceGroupRefused("RESOURCE_GROUP_SLOT_UNAVAILABLE")

    def bind_child_consumer(self, plan: GroupPlan, claim: ChildClaim, consumer: ProcessIdentity) -> ChildClaim:
        """Bind the native ACK to a slot reserved before spawning its child."""
        from dataclasses import replace
        if (claim.group_id != plan.group_id or not isinstance(consumer, ProcessIdentity)
                or claim.binding.consumer is not None):
            raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_INVALID")
        updated = replace(claim.binding, consumer=consumer)
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            retained = connection.execute("SELECT * FROM resource_parent_group_claims WHERE group_id=? AND request_key=?",
                                          (plan.group_id, claim.binding.request_key)).fetchone()
            if (retained is None or retained['state'] != 'active'
                    or retained['demand_json'] != _canonical(claim.demand.record())):
                raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_MISMATCH")
            if retained['binding_json'] == _canonical(updated.record()):
                return ChildClaim(plan.group_id, claim.slot_id, updated, claim.demand)
            if retained['binding_json'] != _canonical(claim.binding.record()):
                raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_MISMATCH")
            changed = connection.execute("UPDATE resource_parent_group_slots SET binding_json=? WHERE group_id=? "
                                         "AND slot_id=? AND state='claimed' AND binding_json=?",
                                         (_canonical(updated.record()), plan.group_id, claim.slot_id,
                                          _canonical(claim.binding.record()))).rowcount
            if changed != 1:
                raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            connection.execute("UPDATE resource_parent_group_claims SET binding_json=? WHERE group_id=? AND request_key=?",
                               (_canonical(updated.record()), plan.group_id, claim.binding.request_key))
        return ChildClaim(plan.group_id, claim.slot_id, updated, claim.demand)

    def finish_child(self, plan: GroupPlan, claim: ChildClaim) -> None:
        if not isinstance(claim, ChildClaim):
            raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_INVALID")
        self._ensure_plan(plan)
        if plan.group_id != claim.group_id or self._native_state(claim.binding.consumer) != "DEAD":
            raise ResourceGroupRefused("RESOURCE_GROUP_CHILD_RETAINED")
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            previous = connection.execute('SELECT * FROM resource_parent_group_claims WHERE group_id=? AND request_key=?',
                (claim.group_id, claim.binding.request_key)).fetchone()
            if (previous is not None and previous['state'] == 'finished'
                    and previous['binding_json'] == _canonical(claim.binding.record())
                    and previous['demand_json'] == _canonical(claim.demand.record())):
                return
            changed = connection.execute(
                "UPDATE resource_parent_group_slots SET state='reserved',binding_json=NULL WHERE group_id=? AND slot_id=? "
                "AND role='child' AND state='claimed' AND binding_json=?",
                (claim.group_id, claim.slot_id, _canonical(claim.binding.record())),
            ).rowcount
            if changed != 1:
                raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_MISMATCH")
            connection.execute("UPDATE resource_parent_group_claims SET state='finished' WHERE group_id=? AND request_key=? "
                               "AND binding_json=? AND state='active'", (claim.group_id, claim.binding.request_key,
                                                                           _canonical(claim.binding.record())))

    def mark_parent_ended(self, plan: GroupPlan, binding: LaunchBinding, proof_sha256: str) -> None:
        self._ensure_binding(plan, binding, parent=True)
        if not _sha(proof_sha256):
            raise ResourceGroupRefused("RESOURCE_GROUP_PARENT_PROOF_INVALID")
        if self._native_state(binding.consumer) != "DEAD":
            raise ResourceGroupRefused("RESOURCE_GROUP_PARENT_RETAINED")
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            changed = connection.execute(
                "UPDATE resource_parent_groups SET state='parent_ended',parent_end_proof_sha256=? WHERE group_id=? "
                "AND state='reserved' AND parent_binding_json=?",
                (proof_sha256, plan.group_id, _canonical(binding.record())),
            ).rowcount
            if changed != 1:
                raise ResourceGroupRefused("RESOURCE_GROUP_PARENT_PROOF_INVALID")

    def close_after_parent_end(self, plan: GroupPlan) -> None:
        """Release all envelopes only after caller-proven parent end and no child."""
        self._ensure_plan(plan)
        with self.queue._connection() as connection:
            self._stored_plan(connection, plan)
            parent = connection.execute("SELECT binding_json FROM resource_parent_group_slots WHERE group_id=? AND role='parent'",
                                        (plan.group_id,)).fetchone()
        if parent is None or not parent["binding_json"]:
            raise ResourceGroupRefused("RESOURCE_GROUP_PARENT_PROOF_INVALID")
        if self._native_state(self._binding(parent["binding_json"]).consumer) != "DEAD":
            raise ResourceGroupRefused("RESOURCE_GROUP_PARENT_RETAINED")
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            group = connection.execute("SELECT * FROM resource_parent_groups WHERE group_id=?", (plan.group_id,)).fetchone()
            if group is None or group["state"] != "parent_ended" or not group["parent_end_proof_sha256"]:
                raise ResourceGroupRefused("RESOURCE_GROUP_PARENT_PROOF_INVALID")
            parent_now = connection.execute("SELECT binding_json FROM resource_parent_group_slots WHERE group_id=? AND role='parent'",
                                            (plan.group_id,)).fetchone()
            if parent_now is None or parent_now["binding_json"] != group["parent_binding_json"]:
                raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            outstanding = connection.execute("SELECT 1 FROM resource_parent_group_slots WHERE group_id=? AND role='child' AND state='claimed'",
                                             (plan.group_id,)).fetchone()
            if outstanding is not None:
                raise ResourceGroupRefused("RESOURCE_GROUP_CHILD_RETAINED")
            rows = connection.execute("SELECT * FROM resource_parent_group_slots WHERE group_id=?", (plan.group_id,)).fetchall()
            for row in rows:
                if connection.execute("UPDATE managed_admissions SET status='released' WHERE sequence=? AND ticket=? "
                                      "AND status IN ('waiting','active')", (row["ticket_sequence"], row["ticket"])).rowcount != 1:
                    raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            connection.execute("UPDATE resource_parent_groups SET state='closed' WHERE group_id=?", (plan.group_id,))

    def release_unissued_hold(self, plan: GroupPlan, proof_sha256: str) -> None:
        """Close a held envelope that never bound a parent, on caller proof that no launch was issued.

        The caller proves through launch authority that the fenced reserving owner issued no
        authorized intent.  This registry independently requires the reserving process dead and
        every slot and admission still free of any intent, binding or consumer.
        """
        self._ensure_plan(plan)
        if not _sha(proof_sha256):
            raise ResourceGroupRefused("RESOURCE_GROUP_PARENT_PROOF_INVALID")
        with self.queue._connection() as connection:
            self._stored_plan(connection, plan)
            owners = connection.execute("SELECT owner_json FROM resource_parent_group_slots WHERE group_id=?",
                                        (plan.group_id,)).fetchall()
        if not owners or any(self._native_state(_parse_identity(json.loads(row["owner_json"]))) != "DEAD" for row in owners):
            raise ResourceGroupRefused("RESOURCE_GROUP_MEMBER_RETAINED")
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            group = connection.execute("SELECT * FROM resource_parent_groups WHERE group_id=?", (plan.group_id,)).fetchone()
            if group is None or group["state"] != "reserved" or group["parent_binding_json"]:
                raise ResourceGroupRefused("RESOURCE_GROUP_NOT_AVAILABLE")
            rows = connection.execute(
                "SELECT s.*,m.launch_intent_id,m.child_host_id,m.child_boot_id,m.child_pid,m.child_start_token "
                "FROM resource_parent_group_slots s JOIN managed_admissions m ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket "
                "WHERE s.group_id=?", (plan.group_id,)).fetchall()
            if (len(rows) != len(plan.members) or len(rows) != len(owners)
                    or any(row["state"] != "reserved" or row["binding_json"] or row["launch_intent_id"] is not None
                           or self._row_consumer(row) is not None for row in rows)):
                raise ResourceGroupRefused("RESOURCE_GROUP_MEMBER_RETAINED")
            for row in rows:
                if connection.execute("UPDATE managed_admissions SET status='released' WHERE sequence=? AND ticket=? "
                                      "AND status IN ('waiting','active')", (row["ticket_sequence"], row["ticket"])).rowcount != 1:
                    raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            connection.execute("UPDATE resource_parent_groups SET state='closed',parent_end_proof_sha256=? WHERE group_id=?",
                               (proof_sha256, plan.group_id))

    def expire_staging(self, plan: GroupPlan, *, now_ns: int | None = None) -> bool:
        """Expire only an entirely unlaunched reservation; retain its original age."""
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        if type(now_ns) is not int:
            raise ResourceGroupRefused("RESOURCE_GROUP_TIME_INVALID")
        self._ensure_plan(plan)
        # Inspect all possible intent/consumer bindings first.  Native and
        # authority proof calls happen before the writer transaction.
        with self.queue._connection() as connection:
            self._stored_plan(connection, plan)
            admissions = connection.execute(
                "SELECT m.*,s.role,s.binding_json FROM managed_admissions m JOIN resource_parent_group_slots s "
                "ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket WHERE s.group_id=?", (plan.group_id,)).fetchall()
        for row in admissions:
            consumer = self._row_consumer(row)
            if consumer is not None:
                raise ResourceGroupRefused("RESOURCE_GROUP_MEMBER_RETAINED")
            if row["launch_intent_id"] is not None:
                lease = LeaseIdentity(row["repository_id"], row["run_id"], row["request_key"], row["launch_intent_id"],
                                      row["generation"], self._row_owner(row))
                if not self.evidence_reader.proves_never_authorized(lease):
                    raise ResourceGroupRefused("RESOURCE_GROUP_PRELAUNCH_PROOF_REQUIRED")
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            group = connection.execute("SELECT * FROM resource_parent_groups WHERE group_id=?", (plan.group_id,)).fetchone()
            if group is None or group["state"] not in {"staging", "reserved"}:
                return False
            current = connection.execute(
                "SELECT m.launch_intent_id,m.child_host_id,m.child_boot_id,m.child_pid,m.child_start_token "
                "FROM managed_admissions m JOIN resource_parent_group_slots s ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket "
                "WHERE s.group_id=?", (plan.group_id,)).fetchall()
            if any(row["launch_intent_id"] is not None or self._row_consumer(row) is not None for row in current):
                raise ResourceGroupRefused("RESOURCE_GROUP_MEMBER_RETAINED")
            launched = connection.execute("SELECT 1 FROM resource_parent_group_slots WHERE group_id=? AND ever_launched=1", (plan.group_id,)).fetchone()
            if now_ns < group["expires_ns"] or launched is not None:
                return False
            rows = connection.execute("SELECT * FROM resource_parent_group_slots WHERE group_id=?", (plan.group_id,)).fetchall()
            for row in rows:
                if connection.execute("UPDATE managed_admissions SET status='released' WHERE sequence=? AND ticket=? "
                                      "AND status IN ('waiting','active')", (row["ticket_sequence"], row["ticket"])).rowcount != 1:
                    raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            connection.execute("UPDATE resource_parent_groups SET state='expired' WHERE group_id=?", (plan.group_id,))
            return True

    def retry_expired_staging(self, plan: GroupPlan) -> GroupReservation:
        """Requeue an expired, wholly unlaunched group with its original age."""
        self._ensure_plan(plan)
        with self.queue._transaction() as connection:
            self._stored_plan(connection, plan)
            group = connection.execute("SELECT * FROM resource_parent_groups WHERE group_id=?", (plan.group_id,)).fetchone()
            if group is None or group["state"] != "expired":
                raise ResourceGroupRefused("RESOURCE_GROUP_RETRY_INVALID")
            if connection.execute("SELECT 1 FROM resource_parent_group_slots WHERE group_id=? AND ever_launched=1",
                                  (plan.group_id,)).fetchone() is not None:
                raise ResourceGroupRefused("RESOURCE_GROUP_RETRY_INVALID")
            slots = connection.execute("SELECT * FROM resource_parent_group_slots WHERE group_id=? ORDER BY ticket_sequence",
                                       (plan.group_id,)).fetchall()
            for slot in slots:
                if connection.execute("UPDATE managed_admissions SET status='waiting',group_age_ns=?,next_recheck_ns=0 "
                                      "WHERE sequence=? AND ticket=? AND status='released'",
                                      (group["age_ns"], slot["ticket_sequence"], slot["ticket"])).rowcount != 1:
                    raise ResourceGroupRefused("RESOURCE_GROUP_CAS_FAILED")
            new_expiry = time.monotonic_ns() + 1_000_000_000
            connection.execute("UPDATE resource_parent_groups SET state='staging',expires_ns=? WHERE group_id=?",
                               (new_expiry, plan.group_id))
        tickets = {slot["slot_id"]: self._ticket(slot) for slot in slots}
        parent = next(member for member in plan.members if member.role == "parent")
        if not self.queue.try_admit(tickets[parent.slot_id]):
            return GroupReservation(plan.group_id, tickets, "staging")
        with self.queue._transaction() as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM managed_admissions m JOIN resource_parent_group_slots s "
                "ON s.ticket_sequence=m.sequence AND s.ticket=m.ticket WHERE s.group_id=? AND m.status='active'",
                (plan.group_id,),
            ).fetchone()[0]
            if active != len(plan.members):
                raise ResourceGroupRefused("RESOURCE_GROUP_ADMISSION_UNCERTAIN")
            connection.execute("UPDATE resource_parent_groups SET state='reserved' WHERE group_id=? AND state='staging'",
                               (plan.group_id,))
        return GroupReservation(plan.group_id, tickets, "reserved")

    def status(self, group_id: str) -> dict:
        with self.queue._connection() as connection:
            group = connection.execute("SELECT * FROM resource_parent_groups WHERE group_id=?", (group_id,)).fetchone()
            if group is None:
                raise ResourceGroupRefused("RESOURCE_GROUP_UNKNOWN")
            slots = connection.execute("SELECT * FROM resource_parent_group_slots WHERE group_id=? ORDER BY slot_id", (group_id,)).fetchall()
            return {"group": dict(group), "slots": [dict(slot) for slot in slots]}

    def _reservation(self, plan: GroupPlan) -> GroupReservation | None:
        with self.queue._connection() as connection:
            group = connection.execute("SELECT * FROM resource_parent_groups WHERE group_id=?", (plan.group_id,)).fetchone()
            if group is None:
                return None
            if group["plan_json"] != _canonical(plan.inventory_record()) or group["inventory_sha256"] != plan.inventory_sha256:
                raise ResourceGroupRefused("RESOURCE_GROUP_PLAN_CONFLICT")
            slots = connection.execute("SELECT * FROM resource_parent_group_slots WHERE group_id=?", (plan.group_id,)).fetchall()
            tickets = {slot["slot_id"]: self._ticket(slot) for slot in slots}
            return GroupReservation(plan.group_id, tickets, group["state"])

    @staticmethod
    def _stored_plan(connection, plan: GroupPlan):
        group = connection.execute("SELECT plan_json,inventory_sha256 FROM resource_parent_groups WHERE group_id=?",
                                   (plan.group_id,)).fetchone()
        if (group is None or group["plan_json"] != _canonical(plan.inventory_record())
                or group["inventory_sha256"] != plan.inventory_sha256):
            raise ResourceGroupRefused("RESOURCE_GROUP_PLAN_CONFLICT")
        return group

    def _native_state(self, identity: ProcessIdentity | None) -> str:
        if identity is None:
            return 'UNKNOWN'
        try:
            result = self.evidence_reader.native_state(identity)
        except Exception as error:
            raise ResourceGroupRefused("RESOURCE_GROUP_NATIVE_PROBE_UNKNOWN") from error
        if result not in {"DEAD", "LIVE", "UNKNOWN"}:
            raise ResourceGroupRefused("RESOURCE_GROUP_NATIVE_PROBE_UNKNOWN")
        return result

    @staticmethod
    def _binding(raw: str) -> LaunchBinding:
        try:
            value = json.loads(raw)
            return LaunchBinding(value["repository_id"], value["run_id"], value["request_key"],
                                 value["launch_intent_id"], value["generation"],
                                 _parse_identity(value["supervisor"]),
                                 None if value["consumer"] is None else _parse_identity(value["consumer"]))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise ResourceGroupRefused("RESOURCE_GROUP_RECORD_INVALID") from error

    @staticmethod
    def _row_owner(row) -> ProcessIdentity:
        try:
            return ProcessIdentity(row["host_id"], row["boot_id"], row["pid"], row["start_token"])
        except (TypeError, ValueError) as error:
            raise ResourceGroupRefused("RESOURCE_GROUP_RECORD_INVALID") from error

    @staticmethod
    def _row_consumer(row) -> ProcessIdentity | None:
        values = (row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"])
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise ResourceGroupRefused("RESOURCE_GROUP_RECORD_INVALID")
        try:
            return ProcessIdentity(*values)
        except (TypeError, ValueError) as error:
            raise ResourceGroupRefused("RESOURCE_GROUP_RECORD_INVALID") from error

    @staticmethod
    def _compatible(requested: ResourceDemand, reserved: ResourceDemand) -> bool:
        return (requested.provider == reserved.provider and requested.provider_units <= reserved.provider_units
                and all(getattr(requested, field) <= getattr(reserved, field)
                        for field in ("cpu", "memory_bytes", "disk_bytes", "io_units", "processes")))

    @staticmethod
    def _ticket(row) -> AdmissionTicket:
        return AdmissionTicket(row["ticket_sequence"], row["ticket"], _parse_identity(json.loads(row["owner_json"])))

    @staticmethod
    def _parse_slot_owner(row) -> ProcessIdentity:
        return _parse_identity(json.loads(row['owner_json']))

    def _slot(self, group_id: str, slot_id: str):
        with self.queue._connection() as connection:
            row = connection.execute("SELECT * FROM resource_parent_group_slots WHERE group_id=? AND slot_id=?", (group_id, slot_id)).fetchone()
            if row is None:
                raise ResourceGroupRefused("RESOURCE_GROUP_RECORD_INVALID")
            return row

    @staticmethod
    def _ensure_plan(plan: GroupPlan) -> None:
        if not isinstance(plan, GroupPlan):
            raise ResourceGroupRefused("RESOURCE_GROUP_PLAN_INVALID")

    @staticmethod
    def _ensure_binding(plan: GroupPlan, binding: LaunchBinding, *, parent: bool) -> None:
        if not isinstance(binding, LaunchBinding) or (binding.repository_id, binding.run_id, binding.generation) != (
                plan.repository_id, plan.run_id, plan.generation):
            raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_MISMATCH")
        if parent and binding.request_key != plan.parent_request_key:
            raise ResourceGroupRefused("RESOURCE_GROUP_BINDING_MISMATCH")
