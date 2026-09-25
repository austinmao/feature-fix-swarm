"""Bindings between the existing shared registry and per-run ControlStore.

ControlStore reads and native identity probes occur outside registry writer
transactions. A dead process alone never establishes an unconsumed lease.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from process_identity import DEAD, ProcessIdentity, probe_identity
from .managed_admission import AdmissionTicket, FencedLeaseRecord, LeaseIdentity, ManagedAdmissionQueue, ManagedAdmissionRefused
from .ownership import assert_owner
from .resource_observation import ResourceDemand
from .state import ControlStore, ControlStoreRefused


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def cold_start_demand(request):
    """Version-one conservative CLI estimates, distinct from remote compute.

    These are explicit local reservations, not measured peaks or provider
    quotas. Runtime peak feedback may raise later policy estimates.
    """
    codex = request.codex_material is not None or request.qualification_material is not None
    claude = request.claude_material is not None or request.claude_qualification_material is not None
    native_review = getattr(request, "native_review_material", None)
    if native_review is not None:
        codex = native_review.native.host == "codex"
        claude = native_review.native.host == "claude"
    provider = "codex" if codex else "claude" if claude else None
    return ResourceDemand(cpu=1, memory_bytes=(512 if provider else 64) * 1024 * 1024,
                          disk_bytes=8 * 1024 * 1024, processes=3 if request.monitor_result else 2,
                          provider=provider, provider_units=1 if provider else 0)


def record_admission_request(store, token, *, activity_id, request_key, material):
    """Freeze the request before taking a shared lease, without a launch debit."""
    request_digest = hashlib.sha256(_encoded(material).encode()).hexdigest()
    queue_key = activity_id + ":" + request_key
    run_key = _encoded({"repository_id": token.repository_id, "run_id": token.run_id})
    with store.transaction() as tx:
        assert_owner(tx, token)
        activity = store._assert_activity_binding(tx, token, activity_id)
        if activity["generation"] != token.generation or activity["state"] not in {"pending", "active"}:
            raise ControlStoreRefused("FENCE_REVOKED")
        owner = tx.execute(
            "SELECT * FROM control_reservations WHERE resource_type='run' AND resource_key=? "
            "AND generation=? AND held=1", (run_key, token.generation),
        ).fetchone()
        if owner is None:
            raise ControlStoreRefused("FENCE_REVOKED")
        payload = {
            "schema": "ffs.shared-admission-request/v1",
            "repository_id": token.repository_id, "run_id": token.run_id,
            "activity_id": activity_id, "request_key": queue_key,
            "dispatch_request_key": request_key, "generation": token.generation,
            "request_sha256": request_digest, "owner_set": owner["owner_set"],
            "supervisor": {key: owner[key] for key in ("host_id", "boot_id", "pid", "start_token")},
        }
        store._record_event_once_tx(tx, token, activity_id, "resource-request:" + queue_key, payload)
    return payload


class SharedResourceCoordinator:
    """Supervisor-owned per-dispatch admission, never a whole-run ticket.

    Demands are supplied by the trusted role/runtime policy, not worker IPC.
    Shared admission occurs before charging a per-run launch. The returned
    tickets must be bound to that intent before any process is spawned.
    """

    def __init__(self, store, token, *, queue=None, poll_seconds=0.1):
        if not 0 < poll_seconds <= 5:
            raise ValueError("bounded admission polling required")
        self.store, self.token = store, token
        self.queue = queue if queue is not None else ManagedAdmissionQueue()
        self.poll_seconds = poll_seconds

    def acquire(self, requests, *, group_key=None):
        """Requests contain activity_id, request_key, material and typed demand."""
        if (not isinstance(requests, tuple) or not requests
                or (len(requests) > 1 and not group_key)
                or any(not isinstance(item.get("demand"), ResourceDemand) for item in requests)):
            raise ControlStoreRefused("SHARED_RESOURCE_REQUEST_INVALID")
        bindings = [record_admission_request(
            self.store, self.token, activity_id=item["activity_id"],
            request_key=item["request_key"], material=item["material"],
        ) for item in requests]
        if len({item["request_key"] for item in bindings}) != len(bindings):
            raise ControlStoreRefused("SHARED_RESOURCE_REQUEST_INVALID")
        group_id = (hashlib.sha256(_encoded([
            self.token.repository_id, self.token.run_id, self.token.generation, group_key,
        ]).encode()).hexdigest() if group_key else None)
        tickets = tuple(self.queue.enqueue(
            state_root=self.store.db_path.parent, repository_id=self.token.repository_id,
            run_id=self.token.run_id, request_key=binding["request_key"],
            generation=self.token.generation, demand=item["demand"],
            group_id=group_id, group_width=len(requests),
        ) for item, binding in zip(requests, bindings))
        from .resource_watchdog import ResourceWatchdog, ResourceWatchdogPolicy, CAPABILITY_FAILURE
        visibility = _AdmissionWatchdogRegistry(self.queue, tickets, requests)
        watchdog = ResourceWatchdog(self.queue._observe, visibility,
                                    policy=ResourceWatchdogPolicy('ffs.resource-watchdog/v1'))
        waiting_started = time.monotonic_ns()
        watchdog.start()
        try:
            while True:
                if (watchdog.fatal_status is not None or any(
                        status.code == CAPABILITY_FAILURE for status in watchdog.latest_statuses)):
                    raise ControlStoreRefused('RESOURCE_CAPABILITY_FAILURE')
                with self.store.transaction() as tx:
                    assert_owner(tx, self.token)
                    for binding in bindings:
                        activity = self.store._assert_activity_binding(tx, self.token, binding["activity_id"])
                        if activity["state"] not in {"pending", "active"} or activity["generation"] != self.token.generation:
                            raise ControlStoreRefused("FENCE_REVOKED")
                states = [self.queue.status(ticket) for ticket in tickets]
                ready_to_check = all(row['status'] == 'active' or row['next_recheck_ns'] <= time.monotonic_ns()
                                     for row in states)
                try:
                    if ready_to_check and all(self.queue.try_admit(ticket) for ticket in tickets):
                        break
                except ManagedAdmissionRefused as error:
                    if error.code != 'RESOURCE_OBSERVATION_UNAVAILABLE':
                        raise
                    # The independent watchdog owns bounded collector validation.
                time.sleep(self.poll_seconds)
        finally:
            if not watchdog.stop(timeout=8):
                raise ControlStoreRefused('RESOURCE_WATCHDOG_UNSETTLED')
        elapsed = time.monotonic_ns() - waiting_started
        budget = self.store.get_run_policy_budget(repository_id=self.token.repository_id,
                                                  run_id=self.token.run_id)
        if budget is not None:
            self.store.record_policy_wait(self.token, kind="capacity", elapsed_ns=elapsed)
        with self.store.transaction() as tx:
            assert_owner(tx, self.token)
            for ticket, binding, item in zip(tickets, bindings, requests):
                self.store._record_event_once_tx(
                    tx, self.token, binding["activity_id"], "resource-lease:" + binding["request_key"],
                    {"schema": "ffs.shared-resource-lease/v1", "sequence": ticket.sequence,
                     "ticket": ticket.ticket, "request": binding, "demand": item["demand"].record(),
                     "group_id": group_id, "group_width": len(requests)},
                )
        return tuple(zip(tickets, bindings))

    def bind_intent(self, reservation, intent_id, *, consumer=None):
        ticket, binding = reservation
        lease = LeaseIdentity(self.token.repository_id, self.token.run_id, binding["request_key"],
                              intent_id, binding["generation"], ticket.owner, consumer)
        self.queue.bind_consumer(ticket, lease)
        return lease

    def release(self, reservation):
        # Queue validates actual consumer death outside its SQLite writer.
        self.queue.release(reservation[0])

    def restore_bound_intent(self, intent_id):
        """Recover an exact same-writer ticket without changing registry authority."""
        try:
            current = ProcessIdentity.current()
            with self.store.read_transaction() as tx:
                assert_owner(tx, self.token)
                intent = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,)).fetchone()
                if intent is None:
                    raise ValueError("missing intent")
                self.store._assert_activity_binding(tx, self.token, intent["activity_id"])
                # Observation of a predecessor is not adoption of its resource ticket.
                if intent["generation"] != self.token.generation:
                    return None

                def event(key):
                    row = tx.execute("SELECT k.payload_hash,e.payload FROM authority_event_keys k "
                        "JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?",
                        (intent["activity_id"], key)).fetchone()
                    if row is None:
                        return None
                    wrapped = json.loads(row["payload"])
                    data = wrapped["data"]
                    if (wrapped["run_id"] != self.token.run_id or wrapped["activity_id"] != intent["activity_id"]
                            or hashlib.sha256(_encoded(data).encode()).hexdigest() != row["payload_hash"]):
                        raise ValueError("event binding changed")
                    return data

                keys = tx.execute("SELECT k.idempotency_key,e.payload FROM authority_event_keys k "
                    "JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? "
                    "AND k.idempotency_key LIKE 'dispatch-request:%'", (intent["activity_id"],)).fetchall()
                keys = [row["idempotency_key"] for row in keys
                        if json.loads(row["payload"])["data"].get("intent_id") == intent_id]
                if len(keys) != 1:
                    raise ValueError("ambiguous dispatch")
                request_key = keys[0].removeprefix("dispatch-request:")
                dispatch = event(keys[0])
                queue_key = intent["activity_id"] + ":" + request_key
                binding = event("resource-request:" + queue_key)
                if binding is None:
                    return None  # Legacy and prepaid group paths use different admission records.
                lease = event("resource-lease:" + queue_key)
                if (dispatch is None or lease is None or dispatch["intent_id"] != intent_id
                        or binding["schema"] != "ffs.shared-admission-request/v1"
                        or binding["repository_id"] != self.token.repository_id
                        or binding["run_id"] != self.token.run_id or binding["activity_id"] != intent["activity_id"]
                        or binding["generation"] != self.token.generation or binding["supervisor"] != asdict(current)
                        or binding["request_key"] != queue_key or binding["dispatch_request_key"] != request_key
                        or binding["request_sha256"] != hashlib.sha256(_encoded(dispatch["request"]).encode()).hexdigest()
                        or lease["schema"] != "ffs.shared-resource-lease/v1" or lease["request"] != binding
                        or type(lease["sequence"]) is not int or not isinstance(lease["ticket"], str)):
                    raise ValueError("retained lease binding changed")
            ticket = AdmissionTicket(lease["sequence"], lease["ticket"], current)
            row = self.queue.status(ticket)
            if (any(row[key] != binding[key] for key in ("repository_id", "run_id", "request_key", "generation"))
                    or {key: row[key] for key in ("host_id", "boot_id", "pid", "start_token")} != asdict(current)
                    or any(row["child_" + key] != intent["child_" + key]
                           for key in ("host_id", "boot_id", "pid", "start_token"))
                    or row["launch_intent_id"] != intent_id or row["child_pid"] is None
                    or Path(row["state_root"]).resolve() != self.store.db_path.parent.resolve()
                    or json.loads(row["demand_json"]) != lease["demand"]
                    or row["group_id"] != lease["group_id"] or row["group_width"] != lease["group_width"]
                    or row["status"] not in {"active", "released"}):
                raise ValueError("registry lease binding changed")
            return None if row["status"] == "released" else (ticket, binding)
        except (KeyError, TypeError, ValueError, ManagedAdmissionRefused) as error:
            raise ControlStoreRefused("SHARED_RESOURCE_LEASE_RESTORE_REQUIRED") from error

    def record_feedback(self, reservation, *, outcome):
        self.queue.record_provider_feedback(reservation[0], outcome=outcome)


class _AdmissionWatchdogRegistry:
    """Visibility only, in the same shared registry as the watched leases."""

    def __init__(self, queue, tickets, requests):
        self.queue, self.tickets, self.requests = queue, tickets, requests
        with queue._transaction() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS resource_watchdog_status '
                               '(scope TEXT PRIMARY KEY, status_json TEXT NOT NULL)')

    def resource_watchdog_targets(self):
        from .resource_watchdog import WatchdogTarget
        for ticket, request in zip(self.tickets, self.requests):
            row = self.queue.status(ticket)
            verdict = row['limiting_resource'] if row['status'] == 'waiting' else None
            yield WatchdogTarget(ticket.ticket, request['demand'], row['last_progress_ns'], verdict)

    def persist_resource_watchdog_status(self, status):
        with self.queue._transaction() as connection:
            connection.execute('INSERT INTO resource_watchdog_status VALUES(?,?) '
                               'ON CONFLICT(scope) DO UPDATE SET status_json=excluded.status_json',
                               (status.scope, _encoded(asdict(status))))


class ControlStoreLeaseEvidenceReader:
    """Prove absence only for a retired exact writer and retained request.

    The registry locates this store from its immutable state_root. No owning
    project checkout or active session is required. Busy or malformed state
    remains unknown; this adapter does not repair or initialize authority.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def read_fenced_lease(self, identity: LeaseIdentity):
        if not isinstance(identity, LeaseIdentity):
            return None
        try:
            if probe_identity(identity.supervisor) != DEAD:
                return None
            view = ControlStore.open_read_only(self.db_path)
            with view.read_transaction() as tx:
                rows = tx.execute(
                    "SELECT k.activity_id,k.payload_hash,e.payload FROM authority_event_keys k "
                    "JOIN control_events e ON e.id=k.event_id WHERE k.idempotency_key=?",
                    ("resource-request:" + identity.request_key,),
                ).fetchall()
                if len(rows) != 1:
                    return None
                wrapped = json.loads(rows[0]["payload"])
                data = wrapped["data"]
                if (wrapped["run_id"] != identity.run_id
                        or wrapped["activity_id"] != rows[0]["activity_id"]
                        or data["schema"] != "ffs.shared-admission-request/v1"
                        or data["repository_id"] != identity.repository_id
                        or data["run_id"] != identity.run_id
                        or data["request_key"] != identity.request_key
                        or data["activity_id"] != rows[0]["activity_id"]
                        or data["generation"] != identity.generation
                        or data["supervisor"] != asdict(identity.supervisor)
                        or hashlib.sha256(_encoded(data).encode()).hexdigest() != rows[0]["payload_hash"]):
                    return None
                context = tx.execute(
                    "SELECT repository_id,run_id,writer_version FROM context_runs "
                    "WHERE repository_id=? AND run_id=?", (identity.repository_id, identity.run_id),
                ).fetchone()
                if context is None or context["writer_version"] != "ffs-supervisor/1":
                    return None
                owners = tx.execute("SELECT * FROM control_reservations WHERE owner_set=?",
                                    (data["owner_set"],)).fetchall()
                if (len(owners) != 3 or {row["resource_type"] for row in owners} != {"run", "workspace", "objective"}
                        or any(row["held"] or row["generation"] != identity.generation
                               or {key: row[key] for key in ("host_id", "boot_id", "pid", "start_token")} != data["supervisor"]
                               for row in owners)):
                    return None
                run_key = _encoded({"repository_id": identity.repository_id, "run_id": identity.run_id})
                if next(row for row in owners if row["resource_type"] == "run")["resource_key"] != run_key:
                    return None
                # The immutable dispatch key is the only valid link to an
                # intent. Any retained intent requires its own full settlement.
                intent = tx.execute(
                    "SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                    (data["activity_id"], "dispatch-request:" + data["dispatch_request_key"]),
                ).fetchone()
                if identity.launch_intent_id is not None or intent is not None:
                    return None
                proof = {"request": data, "owner_rows": [dict(row) for row in owners],
                         "intent_absent": True}
                return FencedLeaseRecord(
                    identity.repository_id, identity.run_id, identity.request_key, None,
                    identity.generation, identity.supervisor, "never_authorized", "generation_fenced",
                    hashlib.sha256(_encoded(proof).encode()).hexdigest(),
                )
        except (ControlStoreRefused, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return None
