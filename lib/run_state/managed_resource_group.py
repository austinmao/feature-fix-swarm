"""Supervisor integration for a frozen native parent's prepaid child envelope."""
from __future__ import annotations

from dataclasses import replace, asdict
import hashlib
import json
import time

from process_identity import ProcessIdentity, probe_identity
from .ownership import assert_owner
from .resource_groups import GroupMember, GroupPlan, LaunchBinding, ResourceParentGroupRegistry
from .shared_resources import SharedResourceCoordinator, record_admission_request, ControlStoreLeaseEvidenceReader
from .state import ControlStoreRefused


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class _Evidence:
    def __init__(self, store):
        self.reader = ControlStoreLeaseEvidenceReader(store.db_path)

    @staticmethod
    def native_state(identity):
        return probe_identity(identity)

    def proves_never_authorized(self, lease):
        proof = self.reader.read_fenced_lease(lease)
        return proof is not None and proof.intent_state == 'never_authorized'


class ManagedParentResourceCoordinator(SharedResourceCoordinator):
    """One native parent plus reusable, resource-derived child slots.

    The shared registry remains the only capacity authority. ControlStore
    requests/intent bindings remain the only launch authority. No new process
    can be spawned by this adapter.
    """

    def __init__(self, original, *, parent_request, inventory, inventory_hash, demand):
        super().__init__(original.store, original.token, queue=original.queue,
                         poll_seconds=original.poll_seconds)
        if (_hash(inventory) != inventory_hash or inventory.get('activity_id') != parent_request.activity_id
                or inventory.get('runtime_identity') != parent_request.runtime_identity
                or inventory.get('repository_id') != self.token.repository_id
                or inventory.get('run_id') != self.token.run_id
                or inventory.get('generation') != self.token.generation
                or not isinstance(inventory.get('plans'), list) or not inventory['plans']):
            raise ControlStoreRefused('RESOURCE_GROUP_INVENTORY_INVALID')
        with self.store.read_transaction() as tx:
            rows = tx.execute('SELECT e.payload FROM control_events e JOIN authority_event_keys k ON k.event_id=e.id '
                              "WHERE k.activity_id=? AND k.idempotency_key LIKE 'prelaunch-plan-inventory:%'",
                              (parent_request.activity_id,)).fetchall()
        if not any(json.loads(row[0]).get('data') == {'inventory_sha256': inventory_hash, 'material': inventory}
                   for row in rows):
            raise ControlStoreRefused('RESOURCE_GROUP_INVENTORY_UNRETAINED')
        self.parent_request_key = parent_request.activity_id + ':' + parent_request.request_key
        self.parent_activity_id = parent_request.activity_id
        self.inventory = inventory
        self.inventory_hash = inventory_hash
        self.registry = ResourceParentGroupRegistry(self.queue, _Evidence(self.store))
        self.plan = None
        self.reservation = None
        self.claims = {}
        self.bindings = {}
        self.demand = demand
        # All children are monitored; qualification may demand less, never more.
        self.child_demand = replace(demand, processes=max(3, demand.processes))
        self.child_width = None

    def _reserve_group(self):
        from .provider_feedback import effective_observation
        from .resource_watchdog import ResourceWatchdog, ResourceWatchdogPolicy, WatchdogTarget, CAPABILITY_FAILURE
        owner = ProcessIdentity.current()
        coordinator = self

        class Visibility:
            def __init__(self):
                with coordinator.queue._transaction() as connection:
                    connection.execute('CREATE TABLE IF NOT EXISTS resource_watchdog_status '
                                       '(scope TEXT PRIMARY KEY, status_json TEXT NOT NULL)')

            def resource_watchdog_targets(self):
                return (WatchdogTarget(coordinator.parent_request_key, coordinator.child_demand),)

            def persist_resource_watchdog_status(self, status):
                with coordinator.queue._transaction() as connection:
                    connection.execute('INSERT INTO resource_watchdog_status VALUES(?,?) '
                        'ON CONFLICT(scope) DO UPDATE SET status_json=excluded.status_json',
                        (status.scope, json.dumps(asdict(status), sort_keys=True)))

        watchdog = ResourceWatchdog(self.queue._observe, Visibility(),
                                    policy=ResourceWatchdogPolicy('ffs.parent-group-watchdog/v1'))
        watchdog.start()
        started = time.monotonic_ns()
        try:
            while True:
                if watchdog.fatal_status or any(row.code == CAPABILITY_FAILURE for row in watchdog.latest_statuses):
                    raise ControlStoreRefused('RESOURCE_CAPABILITY_FAILURE')
                with self.store.transaction() as tx:
                    assert_owner(tx, self.token)
                    activity = self.store._assert_activity_binding(tx, self.token, self.parent_activity_id)
                    if activity['state'] != 'active' or activity['generation'] != self.token.generation:
                        raise ControlStoreRefused('FENCE_REVOKED')
                if self.plan is None:
                    capacity_policy = self.store.get_capacity_policy(
                        repository_id=self.token.repository_id, run_id=self.token.run_id)
                    if capacity_policy is None:
                        raise ControlStoreRefused('RUN_LIMITS_REQUIRED')
                    ceiling = capacity_policy['worker_capacity']
                    try:
                        observation = self.queue._observe()
                    except Exception:
                        time.sleep(self.poll_seconds)
                        continue
                    with self.queue._transaction() as connection:
                        observation = effective_observation(connection, observation, providers=[self.demand.provider],
                            boot_id=owner.boot_id, now_ns=time.monotonic_ns())
                    width = 0
                    for candidate in range(1, min(len(self.inventory['plans']), ceiling) + 1):
                        decisions = self.queue._scheduler.decide_group(
                            [self.demand] + [self.child_demand] * candidate, [], observation, ceilings=self.queue._ceilings)
                        if len(decisions) != candidate + 1 or not all(row.admitted for row in decisions):
                            break
                        width = candidate
                    if not width:
                        time.sleep(min(5, self.poll_seconds * 10))
                        continue
                    self.child_width = width
                    members = (GroupMember('parent', 'parent', self.demand),) + tuple(
                        GroupMember('child-' + str(index), 'child', self.child_demand) for index in range(width))
                    group_id = _hash([self.token.repository_id, self.token.run_id, self.token.generation,
                                      self.parent_request_key, self.inventory_hash])
                    material = {'schema': 'ffs.resource-parent-group/v1', 'group_id': group_id,
                        'repository_id': self.token.repository_id, 'run_id': self.token.run_id,
                        'generation': self.token.generation, 'state_root': str(self.store.db_path.parent),
                        'parent_request_key': self.parent_request_key,
                        'members': [member.record() for member in members], 'plan_inventory_sha256': self.inventory_hash}
                    self.plan = GroupPlan(group_id, self.token.repository_id, self.token.run_id, self.token.generation,
                        str(self.store.db_path.parent), self.parent_request_key, members, _hash(material),
                        time.monotonic_ns() + 30_000_000_000, self.inventory_hash)
                self.reservation = self.registry.reserve(self.plan)
                if self.reservation.state == 'reserved':
                    self.registry.hold_parent_launch(self.plan)
                    break
                if self.registry.expire_staging(self.plan):
                    self.reservation = self.registry.retry_expired_staging(self.plan)
                time.sleep(min(5, self.poll_seconds * 10))
        finally:
            if not watchdog.stop(timeout=8):
                raise ControlStoreRefused('RESOURCE_WATCHDOG_UNSETTLED')
        if self.store.get_run_policy_budget(repository_id=self.token.repository_id, run_id=self.token.run_id):
            self.store.record_policy_wait(self.token, kind='capacity', elapsed_ns=time.monotonic_ns() - started)

    def acquire(self, requests, *, group_key=None):
        if not isinstance(requests, tuple) or not requests:
            raise ControlStoreRefused('SHARED_RESOURCE_REQUEST_INVALID')
        bindings = [record_admission_request(self.store, self.token, activity_id=item['activity_id'],
                    request_key=item['request_key'], material=item['material']) for item in requests]
        parent = len(bindings) == 1 and bindings[0]['request_key'] == self.parent_request_key
        if parent:
            if self.plan is None:
                self._reserve_group()
        elif self.plan is None or len(bindings) > self.child_width:
            raise ControlStoreRefused('RESOURCE_GROUP_CHUNK_REQUIRED')
        if self.reservation is None:
            raise ControlStoreRefused('RESOURCE_PARENT_GROUP_REQUIRED')
        result = []
        for item, binding in zip(requests, bindings):
            if not self.registry._compatible(item['demand'], self.demand if parent else self.child_demand):
                raise ControlStoreRefused('RESOURCE_GROUP_DEMAND_INVALID')
            if not parent:
                with self.store.read_transaction() as tx:
                    child = tx.execute('SELECT parent_activity_id FROM authority_child_bindings WHERE activity_id=?',
                                       (item['activity_id'],)).fetchone()
                if child is None or child[0] != self.parent_activity_id:
                    raise ControlStoreRefused('RESOURCE_GROUP_BINDING_MISMATCH')
            binding = {**binding, 'parent_group': self.plan.group_id, 'is_parent': parent,
                       'group_demand': item['demand'].record()}
            result.append((self.reservation.tickets['parent'], binding))
        return tuple(result)

    def validate_wave(self, manifest):
        """A model may select a runnable subset, never invent its plan scope."""
        import re
        from pathlib import Path
        from .prelaunch_inventory import _phase_bytes
        if (manifest['admission']['activity_id'] != self.parent_activity_id
                or manifest['initial_head'] != self.inventory['initial_head']):
            raise ControlStoreRefused('RESOURCE_GROUP_PLAN_DRIFT')
        phase = Path(manifest['orchestrator_root']) / self.inventory['phase_directory']
        files = _phase_bytes(phase)
        # Summaries are legitimate execution outputs. The admitted plan bytes
        # remain immutable; a later plan cannot acquire an envelope by appearing
        # in the directory after the parent was launched.
        if any(retained['path'] not in files or hashlib.sha256(files[retained['path']]).hexdigest() != retained['sha256']
               for retained in self.inventory['plans']):
            raise ControlStoreRefused('RESOURCE_GROUP_PLAN_DRIFT')
        selected = set()
        for plan in manifest['plans']:
            matches = []
            for retained in self.inventory['plans']:
                metadata = retained['frontmatter']
                stem = Path(retained['path']).stem
                identifiers = {stem[:-5]} if stem.endswith('-PLAN') else set()
                phase_number = re.match(r'\d+(?:\.\d+)*', str(metadata.get('phase', '')))
                number = str(metadata.get('plan', ''))
                if phase_number and number.isdigit():
                    identifiers.add(phase_number[0].zfill(2) + '-' + number.zfill(2))
                if plan['id'] in identifiers:
                    matches.append(retained)
            if len(matches) != 1 or matches[0]['path'] in selected:
                raise ControlStoreRefused('RESOURCE_GROUP_PLAN_UNFROZEN')
            retained = matches[0]
            scope = retained['frontmatter'].get('files_modified', [])
            dependencies = retained['frontmatter'].get('depends_on', [])
            if (not isinstance(scope, list) or not isinstance(dependencies, list)
                    or set(plan['files_modified']) | set(plan['files_deleted']) != set(scope)
                    or set(plan.get('depends_on', [])) != set(str(item) for item in dependencies)):
                raise ControlStoreRefused('RESOURCE_GROUP_PLAN_DRIFT')
            selected.add(retained['path'])

    def bind_intent(self, reservation, intent_id, *, consumer=None):
        from .resource_observation import ResourceDemand
        ticket, data = reservation
        binding = LaunchBinding(self.token.repository_id, self.token.run_id, data['request_key'], intent_id,
                                self.token.generation, ticket.owner, consumer)
        if data['is_parent']:
            self.registry.claim_parent(self.plan, binding)
        elif intent_id not in self.claims:
            self.claims[intent_id] = self.registry.claim_child(self.plan, binding, ResourceDemand(**data['group_demand']))
        elif consumer is not None:
            self.claims[intent_id] = self.registry.bind_child_consumer(self.plan, self.claims[intent_id], consumer)
        self.bindings[data['request_key']] = binding
        return binding

    def assert_spawn_safe(self, reservation):
        """Recheck physical headroom, preserving ordinary target reductions.

        CPU load and provider exploration targets may contract without revoking
        a prepaid group. Missing required physical observations or insufficient
        memory/disk/process headroom cannot authorize another child.
        """
        if self.plan is None or reservation[1]['parent_group'] != self.plan.group_id:
            raise ControlStoreRefused('RESOURCE_PARENT_GROUP_REQUIRED')
        with self.queue._connection() as connection:
            rows = connection.execute('SELECT demand_json,binding_json FROM resource_parent_group_slots WHERE group_id=?',
                                      (self.plan.group_id,)).fetchall()
        pending = []
        for row in rows:
            binding = json.loads(row['binding_json']) if row['binding_json'] else None
            if binding is None or binding['consumer'] is None:
                pending.append(json.loads(row['demand_json']))
        try:
            observation = self.queue._observe()
            observation.validate()
        except Exception as error:
            raise ControlStoreRefused('RESOURCE_CAPABILITY_FAILURE') from error
        dimensions = {'memory_bytes': 'memory_available_bytes', 'disk_bytes': 'disk_available_bytes',
                      'processes': 'process_available', 'io_units': 'io_available_units'}
        for demand_key, observed_key in dimensions.items():
            needed = sum(row[demand_key] for row in pending)
            actual = getattr(observation, observed_key)
            if needed and actual is None:
                raise ControlStoreRefused('RESOURCE_CAPABILITY_FAILURE')
            if needed and actual < needed:
                raise ControlStoreRefused('RESOURCE_GROUP_PHYSICAL_UNSAFE')

    def release(self, reservation):
        _ticket, data = reservation
        binding = self.bindings[data['request_key']]
        if data['is_parent']:
            with self.store.read_transaction() as tx:
                intent = tx.execute('SELECT * FROM authority_launch_intents WHERE id=?', (binding.launch_intent_id,)).fetchone()
            if intent is None or intent['state'] not in {'completed_succeeded', 'completed_failed', 'reconcile_required'}:
                raise ControlStoreRefused('RESOURCE_GROUP_PARENT_PROOF_INVALID')
            self.registry.mark_parent_ended(self.plan, binding, _hash(dict(intent)))
            self.registry.close_after_parent_end(self.plan)
        else:
            self.registry.finish_child(self.plan, self.claims[binding.launch_intent_id])

    def record_feedback(self, reservation, *, outcome):
        # Parent feedback is backed by its released native lease. Child grants
        # retain the prepaid slot; they cannot masquerade as released leases.
        if reservation[1]['is_parent']:
            self.queue.record_provider_feedback(reservation[0], outcome=outcome)


_SETTLED_INTENTS = {'completed_succeeded', 'completed_failed', 'reconcile_required'}


def settle_predecessor_groups(store, token, queue) -> list[dict]:
    """Successor rule: never adopt a predecessor's prepaid tickets; prove its group settled, then close it.

    Every older-generation group of this run is rebuilt from the registry's own
    retained plan.  It closes only when ControlStore shows each bound launch
    intent terminal and the registry's native probes show each consumer dead
    (``finish_child``/``mark_parent_ended``/``close_after_parent_end`` keep
    their own proofs).  Anything else stays reserved and is reported; capacity
    is never refunded on doubt.  The successor then reserves a fresh group at
    its own generation through the ordinary coordinator.
    """
    from .resource_groups import ChildClaim, ResourceGroupRefused
    from .resource_observation import ResourceDemand
    with store.transaction() as tx:
        assert_owner(tx, token)
    registry = ResourceParentGroupRegistry(queue, _Evidence(store))

    def settled(binding):
        with store.read_transaction() as tx:
            intent = tx.execute('SELECT i.state,i.generation,a.repository_id,a.run_id FROM authority_launch_intents i '
                                'JOIN authority_activities a ON a.id=i.activity_id WHERE i.id=?',
                                (binding.launch_intent_id,)).fetchone()
        if (intent is None or intent['state'] not in _SETTLED_INTENTS or intent['generation'] != binding.generation
                or (intent['repository_id'], intent['run_id']) != (token.repository_id, token.run_id)):
            raise ResourceGroupRefused('RESOURCE_GROUP_PARENT_PROOF_INVALID')
        return _hash(dict(intent))

    def unissued(plan):
        with store.read_transaction() as tx:
            activities = tx.execute("SELECT id FROM authority_activities WHERE repository_id=? AND run_id=? "
                                    "AND ? LIKE id || ':%'", (token.repository_id, token.run_id,
                                                              plan.parent_request_key)).fetchall()
            intents = [] if len(activities) != 1 else [dict(row) for row in tx.execute(
                'SELECT id,state,generation FROM authority_launch_intents WHERE activity_id=? AND generation=? ORDER BY id',
                (activities[0]['id'], plan.generation))]
        if len(activities) != 1 or any(intent['state'] != 'never_authorized' for intent in intents):
            raise ResourceGroupRefused('RESOURCE_GROUP_PRELAUNCH_PROOF_REQUIRED')
        return _hash({'activity_id': activities[0]['id'], 'generation': plan.generation, 'intents': intents})

    with queue._connection() as connection:
        groups = connection.execute(
            "SELECT * FROM resource_parent_groups WHERE state IN ('staging','reserved','parent_ended')").fetchall()
    outcomes = []
    for group in groups:
        record = json.loads(group['plan_json'])
        if ((record.get('repository_id'), record.get('run_id')) != (token.repository_id, token.run_id)
                or record.get('state_root') != str(store.db_path.parent) or record.get('generation', 0) >= token.generation):
            continue
        try:
            plan = GroupPlan(record['group_id'], record['repository_id'], record['run_id'], record['generation'],
                             record['state_root'], record['parent_request_key'],
                             tuple(GroupMember(item['slot_id'], item['role'], ResourceDemand(**item['demand']))
                                   for item in record['members']),
                             group['inventory_sha256'], group['expires_ns'], record.get('plan_inventory_sha256'))
            if not group['parent_binding_json']:
                # Never bound: only the registry's own wholly-unlaunched expiry may release it
                # (no intent, no consumer, never held for launch, staging deadline passed).
                if registry.expire_staging(plan):
                    state = 'expired'
                else:
                    # Held for launch, never bound: this fenced generation can issue nothing more, so
                    # authority showing no authorized intent proves the hold was never used.
                    registry.release_unissued_hold(plan, unissued(plan))
                    state = 'closed'
                outcomes.append({'group_id': plan.group_id, 'generation': plan.generation, 'state': state})
                continue
            parent = registry._binding(group['parent_binding_json'])
            proof = settled(parent)
            with queue._connection() as connection:
                claims = connection.execute("SELECT * FROM resource_parent_group_claims WHERE group_id=? AND state='active'",
                                            (plan.group_id,)).fetchall()
            for claim in claims:
                binding = registry._binding(claim['binding_json'])
                settled(binding)
                registry.finish_child(plan, ChildClaim(plan.group_id, claim['slot_id'], binding,
                                                       ResourceDemand(**json.loads(claim['demand_json']))))
            if group['state'] == 'reserved':
                registry.mark_parent_ended(plan, parent, proof)
            registry.close_after_parent_end(plan)
            outcomes.append({'group_id': plan.group_id, 'generation': plan.generation, 'state': 'closed'})
        except ResourceGroupRefused as error:
            outcomes.append({'group_id': group['group_id'], 'generation': record.get('generation'),
                             'state': 'retained', 'code': error.code})
    return outcomes
