"""Durable owner-side GSD wave execution and between-wave integration.

The injected preparation callback owns runtime qualification and activity
creation. It must create the supplied activity UUID in the supplied registered
inventory workspace, qualify and promote it to worker, and return its fully
qualified DispatchRequest. No callback launches
the plan: all requests are prepared before the cohort barrier is entered.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Callable
import uuid

from process_identity import ProcessIdentity
from .ownership import OwnershipRefused, assert_owner
from .run_policy import productive_work
from .state import ControlStoreRefused
from .supervisor import DispatchRequest, SupervisorRefused, _publish, _read_evidence
from .wave_execution import (
    capture_wave_snapshot,
    harvest_scoped_patch,
    integrate_wave_patches,
    prepare_integration_material,
)
from .wave_execution import (
    _current_material, _material_record, _head,
    _canonical_integration, _write_integration_evidence,
)
from .worker_channel import (
    WorkerBinding,
    WorkerChannelServer,
    _MAX_WAVE_REPLY_BYTES,
    parse_gsd_wave_manifest,
)
from .workspace import (
    WorkspacePreparation,
    WorkspaceRefused,
    begin_child_workspace_preparation,
    inspect_workspace,
    prepare_workspace,
    load_input_snapshot,
)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _associate_recovery_members(prefix, plans, prepared_plans, members):
    """Bind a retained cohort by durable key and child identity, never ordinal."""
    prepared_by_plan = {record.get("plan_id"): record for record in prepared_plans}
    members_by_key = {member["request_key"]: member for member in members}
    if (
        len(prepared_by_plan) != len(prepared_plans)
        or len(members_by_key) != len(members)
        or len(plans) != len(prepared_plans)
        or len(plans) != len(members)
    ):
        raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
    associated = []
    for index, plan in enumerate(plans):
        key = prefix + f":plan:{index}"
        retained = prepared_by_plan.get(plan["id"])
        member = members_by_key.get(key)
        if (
            retained is None
            or member is None
            or retained.get("activity_id") != member["activity_id"]
        ):
            raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
        associated.append((plan, retained, member, key))
    return associated


@dataclass(frozen=True)
class WaveChildContext:
    event_id: int
    parent_activity_id: str
    activity_id: str
    request_key: str
    manifest: dict
    plan: dict
    preparation: WorkspacePreparation
    candidate_hash: str
    contract_hash: str
    admission_guard: Callable


class WaveConsumer:
    """Callable accepted by WorkerChannelServer.attach_wave_consumer.

    A durable execution claim deliberately fails closed after interruption.
    Reconciliation may recover evidence, but this consumer never relaunches or
    refunds an incomplete wave, including when only part of a cohort started.
    """

    def __init__(
        self,
        supervisor,
        prepare_child: Callable[[WaveChildContext], DispatchRequest],
        *,
        finish_timeout: float | None = None,
    ):
        if supervisor.worker_channel is None or not callable(prepare_child):
            raise SupervisorRefused("WAVE_CONSUMER_UNAVAILABLE")
        self.supervisor = supervisor
        self.store, self.token = supervisor.store, supervisor.token
        self.channel = supervisor.worker_channel
        self.prepare_child = prepare_child
        self.finish_timeout = finish_timeout
        self._lock = threading.Lock()

    def _integration_authority(self):
        required = (
            "assert_integration_settled",
            "register_integration_intent_tx",
            "mark_workspace_integration_pending_tx",
            "mark_integration_applied_tx",
            "publish_integration_tx",
        )
        if not all(callable(getattr(self.store, name, None)) for name in required):
            raise SupervisorRefused("INTEGRATION_AUTHORITY_UNAVAILABLE")
        return self.store

    def _event(self, tx, event_id):
        if type(event_id) is not int or event_id <= 0:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        assert_owner(tx, self.token)
        row = tx.execute(
            "SELECT k.activity_id,k.idempotency_key,k.payload_hash,e.event_type,e.payload "
            "FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id WHERE e.id=?",
            (event_id,),
        ).fetchone()
        try:
            wrapped = json.loads(row["payload"])
            data = wrapped["data"]
            parent = row["activity_id"]
            intent_id = data["intent_id"]
            peer = ProcessIdentity(**data["peer_identity"])
            manifest, encoded = parse_gsd_wave_manifest(_canonical(data["manifest"]))
        except (TypeError, ValueError, KeyError) as error:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH") from error
        if (
            wrapped
            != {"run_id": self.token.run_id, "activity_id": parent, "data": data}
            or set(data)
            != {
                "operation",
                "body",
                "intent_id",
                "peer_identity",
                "workspace",
                "runtime_identity",
                "manifest",
            }
            or data["operation"] != "gsd-wave-request"
            or asdict(peer) != data["peer_identity"]
            or row["event_type"] != row["idempotency_key"]
            or not row["event_type"].startswith("worker-request:" + intent_id + ":")
            or row["payload_hash"] != hashlib.sha256(_canonical(data)).hexdigest()
            or set(data["body"]) != {"manifest_locator", "manifest_sha256"}
            or data["body"]["manifest_sha256"] != hashlib.sha256(encoded).hexdigest()
        ):
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        registration = tx.execute(
            "SELECT k.payload_hash,e.event_type,e.payload FROM authority_event_keys k "
            "JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?",
            (parent, "worker-registration:" + intent_id),
        ).fetchone()
        claim = tx.execute(
            "SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
            (parent, f"gsd-wave:{event_id}:claimed"),
        ).fetchone()
        try:
            registered = json.loads(registration["payload"])
            registered_binding = registered["data"]
            with self.channel._lock:
                binding = self.channel._primary_bindings.get(intent_id)
            if binding is None and claim is not None:
                # A durable claim belongs to the supervisor, rather than to
                # the socket client that made it.  A fresh supervisor/channel
                # reconstructs only the already-journaled routing identity;
                # it never re-registers, debits, or permits a process.
                binding = WorkerBinding(
                    registered_binding["repository_id"],
                    registered_binding["run_id"],
                    registered_binding["activity_id"],
                    registered_binding["intent_id"],
                    registered_binding["generation"],
                    ProcessIdentity(**registered_binding["identity"]),
                    registered_binding["workspace"],
                    registered_binding["runtime_identity"],
                    registered_binding["candidate_hash"],
                    registered_binding["contract_hash"],
                    tuple(registered_binding["allowed_roles"]),
                    tuple(registered_binding["allowed_workspaces"]),
                    ProcessIdentity(**registered_binding["supervisor_identity"]),
                )
            if binding is None:
                raise KeyError(intent_id)
            expected_registration = json.loads(_canonical(asdict(binding)))
            if claim is None:
                self.channel.assert_authorized_wave_peer(intent_id, peer)
        except (TypeError, ValueError, KeyError) as error:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH") from error
        if (
            registered
            != {
                "run_id": self.token.run_id,
                "activity_id": parent,
                "data": expected_registration,
            }
            or registration["payload_hash"]
            != hashlib.sha256(_canonical(expected_registration)).hexdigest()
            or registration["event_type"] != "worker-registration:" + intent_id
            or binding.activity_id != parent
            or binding.repository_id != self.token.repository_id
            or binding.run_id != self.token.run_id
            or (binding.generation != self.token.generation and claim is None)
            or binding.generation > self.token.generation
            or data["workspace"] != binding.workspace
            or data["runtime_identity"] != binding.runtime_identity
            or manifest["orchestrator_root"] != binding.workspace
            or manifest["admission"]
            != {
                "schema": "ffs.supervisor-admission/v1",
                "available": True,
                "repository_id": self.token.repository_id,
                "run_id": self.token.run_id,
                "activity_id": parent,
                "generation": binding.generation,
                "workspace": binding.workspace,
                "runtime_identity": binding.runtime_identity,
            }
        ):
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        self.store._assert_activity_binding(tx, self.token, parent)
        self.store._assert_activity_ancestry(
            tx, parent, repository_id=self.token.repository_id, run_id=self.token.run_id
        )
        intent = tx.execute(
            "SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,)
        ).fetchone()
        activity = tx.execute(
            "SELECT * FROM authority_activities WHERE id=?", (parent,)
        ).fetchone()
        workspace = tx.execute(
            "SELECT w.* FROM context_workspaces w JOIN authority_child_bindings b ON b.workspace_preparation_id=w.preparation_id "
            "WHERE w.repository_id=? AND w.run_id=? AND b.activity_id=?",
            (self.token.repository_id, self.token.run_id, parent),
        ).fetchone()
        allowed_intents = (
            {"released_to_execute", "reconcile_required"}
            if claim is not None
            else {"released_to_execute"}
        )
        if (
            intent is None
            or intent["activity_id"] != parent
            or intent["generation"] != binding.generation
            or intent["state"] not in allowed_intents
            or not intent["permit_id"]
            or activity["state"] != "active"
            or activity["runtime_tuple_hash"] != binding.runtime_identity
            or {
                "host_id": intent["child_host_id"],
                "boot_id": intent["child_boot_id"],
                "pid": intent["child_pid"],
                "start_token": intent["child_start_token"],
            }
            != asdict(binding.identity)
            or workspace is None
            or workspace["state"] != "ready"
            or not workspace["created_by_ffs"]
            or workspace["generation"]
            not in {binding.generation, self.token.generation}
            or workspace["path"] != binding.workspace
        ):
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        if claim is None:
            self.channel._verify_binding(tx, binding)
        else:
            # Observation of a retained claim never grants worker IPC under
            # the successor.  Match original immutable child/workspace fields
            # while the current owner fence above authorizes settlement only.
            child = tx.execute(
                "SELECT * FROM authority_child_bindings WHERE activity_id=?", (parent,)
            ).fetchone()
            if (
                child is None
                or child["workspace_binding"] != binding.workspace
                or child["candidate_hash"] != binding.candidate_hash
                or child["contract_hash"] != binding.contract_hash
                or child["runtime_identity"] != binding.runtime_identity
                or child["workspace_preparation_id"] != workspace["preparation_id"]
                or child["parent_activity_id"] != workspace["parent_activity_id"]
                or child["role"] != workspace["child_role"]
            ):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        info = Path(binding.workspace).lstat()
        if Path(binding.workspace).is_symlink() or json.loads(
            workspace["native_identity_json"]
        ) != [info.st_dev, info.st_ino]:
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        # Also re-read the no-follow canonical file; durable JSON alone is not
        # authority to replace the originating manifest or workspace.
        observed = WorkerChannelServer._read_wave_manifest(
            binding,
            Path(data["body"]["manifest_locator"]),
            data["body"]["manifest_sha256"],
        )
        if observed != manifest:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        return parent, manifest, workspace["preparation_id"], row["payload_hash"]

    def _retained(self, tx, parent, key):
        row = tx.execute(
            "SELECT k.payload_hash,e.event_type,e.payload FROM authority_event_keys k "
            "JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.idempotency_key=?",
            (parent, key),
        ).fetchone()
        if row is None:
            return None
        try:
            wrapped = json.loads(row["payload"])
            data = wrapped["data"]
        except (TypeError, ValueError, KeyError) as error:
            raise SupervisorRefused("WAVE_EVIDENCE_CHANGED") from error
        if (
            wrapped
            != {"run_id": self.token.run_id, "activity_id": parent, "data": data}
            or row["event_type"] != key
            or row["payload_hash"] != hashlib.sha256(_canonical(data)).hexdigest()
        ):
            raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
        return data

    def _settled_requests(self, intent_id: str) -> bool:
        """Return whether every durable wave request for ``intent_id`` is terminal.

        This deliberately reads the journal rather than the in-memory consumer
        lock: a request can exist before this process acquires that lock, and a
        subsequent request can be queued as another one finishes.
        """
        if not isinstance(intent_id, str) or not intent_id:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        request_prefix = f"worker-request:{intent_id}:"
        evidence_to_verify = []
        with self.store.read_transaction() as tx:
            assert_owner(tx, self.token)
            rows = tx.execute(
                "SELECT k.activity_id,k.event_id,k.idempotency_key,e.event_type,e.payload "
                "FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                "WHERE k.idempotency_key LIKE ?",
                (request_prefix + "%",),
            ).fetchall()
            for row in rows:
                try:
                    wrapped = json.loads(row["payload"])
                    data = wrapped["data"]
                except (TypeError, ValueError, KeyError) as error:
                    raise SupervisorRefused("IPC_SCOPE_MISMATCH") from error
                if (
                    not isinstance(wrapped, dict)
                    or not isinstance(data, dict)
                    or wrapped
                    != {
                        "run_id": self.token.run_id,
                        "activity_id": row["activity_id"],
                        "data": data,
                    }
                    or row["event_type"] != row["idempotency_key"]
                    or not row["idempotency_key"].startswith(request_prefix)
                    or wrapped.get("run_id") != self.token.run_id
                    or wrapped.get("activity_id") != row["activity_id"]
                    or data.get("operation") != "gsd-wave-request"
                    or data.get("intent_id") != intent_id
                ):
                    continue
                prefix = f"gsd-wave:{row['event_id']}"
                reply = self._retained(tx, row["activity_id"], prefix + ":reply")
                if reply is not None:
                    evidence = reply.get("evidence")
                    if not isinstance(evidence, dict):
                        raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
                    evidence_to_verify.append((evidence, reply.get("reply")))
                    continue
                refused = self._retained(tx, row["activity_id"], prefix + ":refused")
                if (
                    refused is not None
                    and refused.get("event_id") == row["event_id"]
                    and isinstance(refused.get("code"), str)
                    and refused["code"]
                ):
                    continue
                return False
        # Hash files outside the authority transaction. Journal records are
        # append-only; their captured references are checked before returning.
        for evidence, expected in evidence_to_verify:
            raw = _read_evidence(Path(evidence.get("locator", "")))
            if (
                hashlib.sha256(raw).hexdigest() != evidence.get("sha256")
                or json.loads(raw) != expected
            ):
                raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
        return True

    def wait_for_idle(self, *, intent_id: str, timeout: float) -> None:
        """Keep the issuer's intent live until its supervisor-owned work settles.

        The socket/file client may disappear after claim.  Its process exit
        cannot race authority settlement while this consumer still owns the
        wave.  Waiting holds no ControlStore or workspace lock.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if self._settled_requests(intent_id):
                    return
            except ControlStoreRefused as error:
                if error.code != "STORE_BUSY":
                    raise
                # A consumer may hold the authority fence across integration.
                # Contention is pending work, bounded by the original deadline.
            if time.monotonic() >= deadline:
                raise SupervisorRefused("WAVE_SETTLEMENT_TIMEOUT")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def __call__(self, event_id: int) -> dict:
        with self._lock:
            try:
                return self._consume(event_id)
            except Exception as error:
                # Preserve the first refusal without granting a later caller
                # any new execution or refund authority.
                try:
                    with self.store.transaction() as tx:
                        parent, _manifest, _preparation, _digest = self._event(
                            tx, event_id
                        )
                        prefix = f"gsd-wave:{event_id}"
                        if (
                            self._retained(tx, parent, prefix + ":claimed") is not None
                            and self._retained(tx, parent, prefix + ":refused") is None
                        ):
                            self.store._record_event_once_tx(
                                tx,
                                self.token,
                                parent,
                                prefix + ":refused",
                                {
                                    "event_id": event_id,
                                    "code": getattr(
                                        error, "code", "WAVE_EXECUTION_INTERRUPTED"
                                    ),
                                },
                            )
                except Exception:
                    # Revocation or broken evidence must never authorize an
                    # extra event merely to report the earlier failure.
                    pass
                raise

    def _assert_dependencies(self, tx, parent, manifest, event_id):
        plan_ids = {plan["id"] for plan in manifest["plans"]}
        required = {
            dependency
            for plan in manifest["plans"]
            for dependency in plan.get("depends_on", [])
        }
        if required & plan_ids:
            raise SupervisorRefused("WAVE_COHORT_CAPABILITY_UNAVAILABLE")
        if not required:
            return
        originating_intent = json.loads(
            tx.execute(
                "SELECT payload FROM control_events WHERE id=?",
                (event_id,),
            ).fetchone()["payload"]
        )["data"]["intent_id"]
        completed = set()
        keys = tx.execute(
            "SELECT idempotency_key FROM authority_event_keys WHERE activity_id=? AND idempotency_key LIKE 'gsd-wave:%:reply'",
            (parent,),
        ).fetchall()
        for row in keys:
            match = re.fullmatch(r"gsd-wave:([0-9]+):reply", row["idempotency_key"])
            if match is None:
                continue
            source_key = tx.execute(
                "SELECT idempotency_key FROM authority_event_keys WHERE activity_id=? AND event_id=?",
                (parent, int(match[1])),
            ).fetchone()
            if source_key is None:
                raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
            source = self._retained(tx, parent, source_key["idempotency_key"])
            prior_manifest, _encoded = parse_gsd_wave_manifest(
                _canonical(source["manifest"])
            )
            if (
                source["operation"] != "gsd-wave-request"
                or source["intent_id"] != originating_intent
                or prior_manifest["phase"] != manifest["phase"]
                or prior_manifest["wave"] >= manifest["wave"]
                or prior_manifest["initial_head"] != manifest["initial_head"]
                or prior_manifest["admission"] != manifest["admission"]
            ):
                continue
            prior = self._retained(tx, parent, row["idempotency_key"])
            integrated = self._retained(tx, parent, f"gsd-wave:{match[1]}:integrated")
            raw = _read_evidence(Path(prior["evidence"]["locator"]))
            if (
                hashlib.sha256(raw).hexdigest() != prior["evidence"]["sha256"]
                or json.loads(raw) != prior["reply"]
                or integrated is None
            ):
                raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
            integration_raw = _read_evidence(Path(integrated["evidence"]["locator"]))
            if (
                hashlib.sha256(integration_raw).hexdigest()
                != integrated["evidence"]["sha256"]
                or json.loads(integration_raw) != integrated["material"]
            ):
                raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
            completed.update(
                result["plan_id"]
                for result in prior["reply"]["results"]
                if result["status"] == "complete"
            )
        if not required.issubset(completed):
            raise SupervisorRefused("WAVE_DEPENDENCY_UNSATISFIED")

    def _consume(self, event_id):
        prefix = f"gsd-wave:{event_id}"
        with self.store.transaction() as tx:
            parent, manifest, preparation_id, request_hash = self._event(tx, event_id)
            retained = self._retained(tx, parent, prefix + ":reply")
            if retained is not None:
                evidence = retained["evidence"]
                raw = _read_evidence(Path(evidence["locator"]))
                if (
                    hashlib.sha256(raw).hexdigest() != evidence["sha256"]
                    or json.loads(raw) != retained["reply"]
                ):
                    raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
                return retained["reply"]
            claimed = self._retained(tx, parent, prefix + ":claimed") is not None
            refused = self._retained(tx, parent, prefix + ":refused")
            cohort = tx.execute(
                "SELECT 1 FROM authority_launch_cohorts WHERE repository_id=? AND run_id=? AND request_key=?",
                (self.token.repository_id, self.token.run_id, prefix),
            ).fetchone()
            # __call__ durably records a typed refusal after a claim.  A replay
            # before admission must return that original policy result, rather
            # than falsely treating its no-cohort state as recoverable work.
            if refused is not None and (not claimed or cohort is None):
                if (
                    refused.get("event_id") != event_id
                    or not isinstance(refused.get("code"), str)
                    or not refused["code"]
                ):
                    raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
                raise SupervisorRefused(refused["code"])
            recovering = claimed
            if (
                not callable(getattr(self.supervisor, "launch_cohort", None))
                or manifest["commit_mode"] != "patches"
            ):
                raise SupervisorRefused("WAVE_COHORT_CAPABILITY_UNAVAILABLE")
            self._assert_dependencies(tx, parent, manifest, event_id)
            if not recovering:
                self.store._record_event_once_tx(
                    tx,
                    self.token,
                    parent,
                    prefix + ":claimed",
                    {
                        "event_id": event_id,
                        "request_sha256": request_hash,
                    },
                )

        if recovering:
            return self._recover_claimed(
                event_id, parent, manifest, preparation_id, request_hash
            )

        def guard(tx):
            current = self._event(tx, event_id)
            if current != (parent, manifest, preparation_id, request_hash):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")

        def check():
            with self.store.transaction() as tx:
                guard(tx)

        # Snapshot capture performs its own ownership transactions and reads;
        # its read_transaction contract forbids nesting in a writer fence.
        with productive_work(self.store, self.token, kind="preparation"):
            check()
            from .managed_resource_group import ManagedParentResourceCoordinator
            coordinator = self.supervisor.shared_resource_coordinator
            if isinstance(coordinator, ManagedParentResourceCoordinator):
                coordinator.validate_wave(manifest)
            self._integration_authority().assert_integration_settled(
                self.token,
                manifest["orchestrator_root"],
            )
            preparation = inspect_workspace(self.store, preparation_id)
            snapshot = capture_wave_snapshot(
                self.store,
                self.token,
                preparation,
                manifest,
                self.supervisor.evidence_root,
            )
            contexts, requests = [], []
            for index, plan in enumerate(manifest["plans"]):
                key = prefix + f":plan:{index}"
                check()
                pending = begin_child_workspace_preparation(
                    self.store,
                    self.token,
                    parent_activity_id=parent,
                    request_key=key,
                    role="inventory",
                    base_commit=manifest["initial_head"],
                    selected_input_manifest=snapshot.manifest,
                    repository_path=preparation.repository_path,
                    admission_guard=guard,
                )
                ready = prepare_workspace(
                    self.store,
                    self.token,
                    pending,
                    input_snapshot=snapshot,
                    admission_guard=guard,
                )
                context = WaveChildContext(
                    event_id,
                    parent,
                    str(uuid.uuid4()),
                    key,
                    manifest,
                    plan,
                    ready,
                    snapshot.input_digest,
                    hashlib.sha256(_canonical(plan)).hexdigest(),
                    guard,
                )
                check()
                request = self.prepare_child(context)
                with self.store.transaction() as tx:
                    guard(tx)
                    child = tx.execute(
                        "SELECT a.*,b.parent_activity_id,b.workspace_binding,b.workspace_preparation_id,"
                        "b.candidate_hash,b.contract_hash,b.runtime_identity,b.role,w.child_role FROM authority_activities a "
                        "JOIN authority_child_bindings b ON b.activity_id=a.id "
                        "JOIN context_workspaces w ON w.preparation_id=b.workspace_preparation_id WHERE a.id=?",
                        (context.activity_id,),
                    ).fetchone()
                    if (
                        not isinstance(request, DispatchRequest)
                        or child is None
                        or request.activity_id != context.activity_id
                        or request.request_key != key
                        or request.workspace != str(ready.path)
                        or request.expected_head != manifest["initial_head"]
                        or request.contract_hash != context.contract_hash
                        or child["request_key"] != key
                        or child["parent_activity_id"] != parent
                        or child["workspace_preparation_id"] != ready.id
                        or child["workspace_binding"] != str(ready.path)
                        or child["candidate_hash"] != context.candidate_hash
                        or child["contract_hash"] != context.contract_hash
                        or child["role"] != "worker"
                        or child["child_role"] != "worker"
                        or child["state"] not in {"pending", "active"}
                        or child["runtime_identity"] != request.runtime_identity
                        or child["runtime_tuple_hash"] != request.runtime_identity
                    ):
                        raise SupervisorRefused("WAVE_CHILD_BINDING_MISMATCH")
                contexts.append(context)
                requests.append(replace(request, monitor_result=True))
            with self.store.transaction() as tx:
                guard(tx)
                self.store._record_event_once_tx(
                    tx,
                    self.token,
                    parent,
                    prefix + ":prepared",
                    {
                        "input_digest": snapshot.input_digest,
                        "initial_head": manifest["initial_head"],
                        "plans": [
                            {
                                "plan_id": context.plan["id"],
                                "activity_id": context.activity_id,
                                "workspace_preparation_id": context.preparation.id,
                                "runtime_identity": request.runtime_identity,
                                "runtime_receipt_sha256": request.runtime_receipt_sha256,
                            }
                            for context, request in zip(contexts, requests, strict=True)
                        ],
                    },
                )
        try:
            if isinstance(coordinator, ManagedParentResourceCoordinator):
                width = coordinator.child_width
                if not width:
                    raise SupervisorRefused('RESOURCE_PARENT_GROUP_REQUIRED')
                handles = []
                chunks = [tuple(requests[index:index + width]) for index in range(0, len(requests), width)]
                with self.store.transaction() as tx:
                    guard(tx)
                    self.store._record_event_once_tx(tx, self.token, parent, prefix + ':resource-chunks',
                        {'group_id': coordinator.plan.group_id, 'inventory_sha256': coordinator.inventory_hash,
                         'chunks': [[request.request_key for request in chunk] for chunk in chunks]})
                for index, chunk in enumerate(chunks):
                    chunk_handles = self.supervisor.launch_cohort(chunk,
                        request_key=prefix + ':chunk:' + str(index), admission_guard=guard)
                    uncertain = False
                    for handle in chunk_handles:
                        self.supervisor.finish(handle, timeout=self.finish_timeout)
                        with self.store.read_transaction() as tx:
                            row = tx.execute('SELECT state FROM authority_launch_intents WHERE id=?',
                                             (handle.intent_id,)).fetchone()
                        uncertain |= row is None or row['state'] not in {'completed_succeeded', 'completed_failed'}
                    handles.extend(chunk_handles)
                    if uncertain:
                        raise SupervisorRefused('WAVE_RESULT_UNCERTAIN')
            else:
                handles = self.supervisor.launch_cohort(
                    tuple(requests), request_key=prefix, admission_guard=guard
                )
        except OwnershipRefused as error:
            if error.code in {
                "WORKER_CAPACITY_EXHAUSTED",
                "DISPATCH_BUDGET_EXHAUSTED",
                "TOKEN_BUDGET_EXHAUSTED",
            }:
                raise SupervisorRefused("WAVE_COHORT_CAPABILITY_UNAVAILABLE") from error
            raise
        return self._settle_wave(
            event_id,
            parent,
            manifest,
            preparation_id,
            request_hash,
            snapshot,
            contexts,
            handles,
        )

    def _recover_claimed(
        self, event_id, parent, manifest, preparation_id, request_hash
    ):
        """Observe exactly the retained cohort; never prepare or launch again."""
        with productive_work(self.store, self.token, kind="recovery"):
            prefix = f"gsd-wave:{event_id}"
            with self.store.transaction() as tx:
                if self._event(tx, event_id) != (
                    parent,
                    manifest,
                    preparation_id,
                    request_hash,
                ):
                    raise SupervisorRefused("IPC_SCOPE_MISMATCH")
                prepared = self._retained(tx, parent, prefix + ":prepared")
                resource_chunks = self._retained(tx, parent, prefix + ':resource-chunks')
                if prepared is None:
                    raise SupervisorRefused("WAVE_RECONCILIATION_REQUIRED")
                if resource_chunks is None:
                    chunk_keys = [(prefix, None)]
                else:
                    if (not isinstance(resource_chunks, dict)
                            or set(resource_chunks) != {'group_id', 'inventory_sha256', 'chunks'}
                            or not isinstance(resource_chunks['chunks'], list)
                            or not 1 <= len(resource_chunks['chunks']) <= len(manifest['plans'])
                            or any(not isinstance(chunk, list) or not chunk for chunk in resource_chunks['chunks'])
                            or [key for chunk in resource_chunks['chunks'] for key in chunk] !=
                               [prefix + ':plan:' + str(index) for index in range(len(manifest['plans']))]):
                        raise SupervisorRefused('WAVE_EVIDENCE_CHANGED')
                    chunk_keys = [(prefix + ':chunk:' + str(index), keys)
                                  for index, keys in enumerate(resource_chunks['chunks'])]
                members = []
                for cohort_key, keys in chunk_keys:
                    cohort = tx.execute(
                        "SELECT * FROM authority_launch_cohorts WHERE repository_id=? AND run_id=? AND request_key=?",
                        (self.token.repository_id, self.token.run_id, cohort_key),
                    ).fetchone()
                    if cohort is None or cohort['state'] != 'released_to_execute':
                        # No fresh process is issued while replaying a partial
                        # chunk dispatch; its saved continuation needs recovery.
                        raise SupervisorRefused('WAVE_RECONCILIATION_REQUIRED')
                    retained_members = tx.execute(
                        'SELECT * FROM authority_launch_cohort_members WHERE cohort_id=? ORDER BY member_ordinal',
                        (cohort['id'],)).fetchall()
                    if keys is not None and {row['request_key'] for row in retained_members} != set(keys):
                        raise SupervisorRefused('WAVE_EVIDENCE_CHANGED')
                    members.extend(retained_members)
            if len(members) != len(manifest["plans"]) or len(prepared["plans"]) != len(
                members
            ):
                raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
            contexts, handles, snapshot = [], [], None
            for plan, retained, member, key in _associate_recovery_members(
                prefix, manifest["plans"], prepared["plans"], members
            ):
                if (
                    retained["plan_id"] != plan["id"]
                    or retained["activity_id"] != member["activity_id"]
                    or member["request_key"] != key
                    or not member["acknowledgement_id"]
                    or member["managed_input_sha256"] != prepared["input_digest"]
                    or member["runtime_receipt_sha256"]
                    != retained["runtime_receipt_sha256"]
                ):
                    raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
                ready = inspect_workspace(
                    self.store, retained["workspace_preparation_id"]
                )
                child_snapshot = load_input_snapshot(self.store, ready)
                if (
                    child_snapshot is None
                    or child_snapshot.input_digest != prepared["input_digest"]
                ):
                    raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
                snapshot = child_snapshot
                with self.store.read_transaction() as tx:
                    child = tx.execute(
                        "SELECT * FROM authority_child_bindings WHERE activity_id=?",
                        (member["activity_id"],),
                    ).fetchone()
                    if (
                        child is None
                        or child["parent_activity_id"] != parent
                        or child["workspace_preparation_id"] != ready.id
                        or child["workspace_binding"] != str(ready.path)
                        or child["candidate_hash"] != snapshot.input_digest
                        or child["contract_hash"]
                        != hashlib.sha256(_canonical(plan)).hexdigest()
                        or child["runtime_identity"] != retained["runtime_identity"]
                    ):
                        raise SupervisorRefused("WAVE_CHILD_BINDING_MISMATCH")
                contexts.append(
                    WaveChildContext(
                        event_id,
                        parent,
                        member["activity_id"],
                        key,
                        manifest,
                        plan,
                        ready,
                        snapshot.input_digest,
                        child["contract_hash"],
                        None,
                    )
                )
                handles.append(self.supervisor.resume_monitored(member["intent_id"]))
        return self._settle_wave(
            event_id,
            parent,
            manifest,
            preparation_id,
            request_hash,
            snapshot,
            contexts,
            handles,
        )

    def _settle_wave(
        self,
        event_id,
        parent,
        manifest,
        preparation_id,
        request_hash,
        snapshot,
        contexts,
        handles,
    ):
        prefix = f"gsd-wave:{event_id}"

        def guard(tx):
            if self._event(tx, event_id) != (
                parent,
                manifest,
                preparation_id,
                request_hash,
            ):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")

        def check():
            with self.store.transaction() as tx:
                guard(tx)

        results, uncertain = [], None
        for context, handle in zip(contexts, handles, strict=True):
            try:
                check()
                result = self.supervisor.finish(handle, timeout=self.finish_timeout)
                with self.store.transaction() as tx:
                    guard(tx)
                    intent = tx.execute(
                        "SELECT state FROM authority_launch_intents WHERE id=?",
                        (handle.intent_id,),
                    ).fetchone()
                    if intent is None or intent["state"] not in {
                        "completed_succeeded",
                        "completed_failed",
                    }:
                        raise SupervisorRefused("WAVE_RESULT_UNCERTAIN")
                status, patch, changed, summary = (
                    "failed",
                    "",
                    [],
                    "Plan process failed.",
                )
                patch_evidence = None
                if intent["state"] == "completed_succeeded":
                    try:
                        # Harvesting reads and hashes a workspace and can be slow.
                        # The owner fence protects authority publication, not this
                        # immutable staging step; recheck it immediately before
                        # the later terminal publication.
                        check()
                        with productive_work(self.store, self.token, kind="harvest"):
                            harvested = harvest_scoped_patch(
                                context.preparation.path,
                                manifest["initial_head"],
                                context.plan["files_modified"],
                                context.plan["files_deleted"],
                                self.supervisor.evidence_root,
                                baseline_snapshot=snapshot,
                            )
                        status, patch, changed = (
                            "complete",
                            harvested.patch,
                            list(harvested.changed_files),
                        )
                        patch_evidence = {
                            "locator": str(harvested.evidence_path),
                            "sha256": harvested.sha256,
                        }
                        summary = (
                            "Plan completed."
                            if changed
                            else "Plan completed without changes."
                        )
                    except WorkspaceRefused as error:
                        summary = "Plan patch refused: " + error.code
                with self.store.fenced_operation(self.token):
                    check()
                    with self.store.transaction() as tx:
                        activity = tx.execute(
                            "SELECT state FROM authority_activities WHERE id=?",
                            (context.activity_id,),
                        ).fetchone()
                    terminal = "succeeded" if status == "complete" else "failed"
                    if activity["state"] == "active":
                        self.store.transition_activity(
                            self.token,
                            context.activity_id,
                            expected="active",
                            new=terminal,
                            result=result["evidence"],
                            reason=summary,
                        )
                    elif activity["state"] != terminal:
                        raise SupervisorRefused("WAVE_RESULT_UNCERTAIN")
                with self.store.transaction() as tx:
                    guard(tx)
                    self.store._record_event_once_tx(
                        tx,
                        self.token,
                        parent,
                        context.request_key + ":result",
                        {
                            "plan_id": context.plan["id"],
                            "activity_id": context.activity_id,
                            "intent_id": handle.intent_id,
                            "status": status,
                            "process_evidence": result["evidence"],
                            "patch_evidence": patch_evidence,
                        },
                    )
                results.append(
                    {
                        "plan_id": context.plan["id"],
                        "status": status,
                        "summary": summary,
                        "changed_files": changed,
                        "patch": patch,
                    }
                )
            except Exception as error:
                uncertain = uncertain or error
        if uncertain is not None:
            check()
            self.store.record_event_once(
                self.token,
                parent,
                prefix + ":uncertain",
                {
                    "event_id": event_id,
                    "code": getattr(uncertain, "code", "WAVE_RESULT_UNCERTAIN"),
                },
            )
            raise SupervisorRefused("WAVE_RESULT_UNCERTAIN") from uncertain
        reply = {
            "schema": manifest["schema"],
            "mode": manifest["mode"],
            "wave": manifest["wave"],
            "initial_head": manifest["initial_head"],
            "apply_between_waves": True,
            "results": results,
        }
        if len(_canonical(reply)) > _MAX_WAVE_REPLY_BYTES:
            raise SupervisorRefused("IPC_MESSAGE_TOO_LARGE")
        from run_context import workspace_effect_lock
        with self.store.transaction() as tx:
            guard(tx)
            common_dir = tx.execute(
                "SELECT common_dir FROM context_workspaces WHERE preparation_id=?",
                (preparation_id,),
            ).fetchone()[0]
        # Acquire only the preparation-specific effect lock across Git. The
        # authority remains available to other workspaces and child settlement.
        with (
            productive_work(self.store, self.token, kind="integration"),
            workspace_effect_lock(
                Path(common_dir), repository_id=self.token.repository_id,
                run_id=self.token.run_id, preparation_id=preparation_id,
            ),
        ):
            return self._integrate_results(event_id, parent, prefix, manifest,
                                           results, reply, guard, check)

    def _integrate_results(self, event_id, parent, prefix, manifest, results, reply, guard, check):
        # Hash the exact before/after state in a disposable index before either
        # the pending marker or git apply.  No authority lock spans this work.
        from .integration_journal import read_intent, quarantine
        retained_journal = read_intent(self.store, self.token, wave_key=prefix)
        with productive_work(self.store, self.token, kind="integration"):
            if retained_journal is None:
                material_intent = prepare_integration_material(
                    Path(manifest["orchestrator_root"]),
                    manifest["initial_head"], results, self.supervisor.evidence_root,
                )
            else:
                retained_contract = json.loads(retained_journal["contract_json"])
                material_intent = {
                    "before": retained_contract["before"],
                    "expected_after": retained_contract["expected_after"],
                }
                if retained_journal["state"] == "quarantined":
                    raise SupervisorRefused("WAVE_INTEGRATION_RECONCILIATION_REQUIRED")
        observed = {
            path: _material_record(_current_material(Path(manifest["orchestrator_root"]), path))
            for path in material_intent["before"]
        }
        already_applied = retained_journal is not None and observed == material_intent["expected_after"]
        if (_head(Path(manifest["orchestrator_root"])) != manifest["initial_head"]
                or (observed != material_intent["before"] and not already_applied)):
            if retained_journal is not None:
                quarantine(self.store, self.token, wave_key=prefix, reason="mixed-or-changed-material")
            raise SupervisorRefused("WAVE_INTEGRATION_RECONCILIATION_REQUIRED")
        with productive_work(self.store, self.token, kind="integration"):
            check()
            integration_authority = self._integration_authority()
            with self.store.transaction() as tx:
                guard(tx)
                integrated = self._retained(tx, parent, prefix + ":integrated")
                integration_intent = self._retained(
                    tx, parent, prefix + ":integration-intent"
                )
                if integration_intent is not None and retained_journal is None:
                    raise SupervisorRefused("WAVE_INTEGRATION_RECONCILIATION_REQUIRED")
                journal = integration_authority.register_integration_intent_tx(
                    tx,
                    self.token,
                    prefix,
                    manifest["orchestrator_root"],
                    material_intent["before"],
                    material_intent["expected_after"],
                    {
                        "initial_head": manifest["initial_head"],
                        "plans": [result["plan_id"] for result in results],
                    },
                )
                integration_authority.mark_workspace_integration_pending_tx(
                    tx,
                    self.token,
                    journal,
                    manifest["orchestrator_root"],
                )
                self.store._record_event_once_tx(
                    tx,
                    self.token,
                    parent,
                    prefix + ":integration-intent",
                    {
                        "event_id": event_id,
                        "workspace": manifest["orchestrator_root"],
                        "initial_head": manifest["initial_head"],
                        "before": material_intent["before"],
                        "expected_after": material_intent["expected_after"],
                        "patches": [
                            {
                                "plan_id": result["plan_id"],
                                "status": result["status"],
                                "patch_sha256": hashlib.sha256(
                                    result["patch"].encode()
                                ).hexdigest(),
                            }
                            for result in results
                        ],
                    },
                )
            if integrated is None and already_applied:
                payload = b"".join(result["patch"].encode() for result in results
                                   if result["status"] == "complete")
                material = {
                    "schema": "ffs.wave-integration/v1",
                    "workspace": manifest["orchestrator_root"],
                    "initial_head": manifest["initial_head"],
                    "patch_sha256": hashlib.sha256(payload).hexdigest(),
                    "changed_files": sorted(material_intent["before"]),
                    "before": material_intent["before"],
                    "after": material_intent["expected_after"],
                }
                path, digest = _write_integration_evidence(
                    self.supervisor.evidence_root, _canonical_integration(material),
                )
                integration = {"locator": str(path), "sha256": digest, "material": material}
            elif integrated is None:
                integration = integrate_wave_patches(
                    Path(manifest["orchestrator_root"]),
                    manifest["initial_head"],
                    results,
                    self.supervisor.evidence_root,
                )
            else:
                evidence = integrated["evidence"]
                raw = _read_evidence(Path(evidence["locator"]))
                material = integrated["material"]
                if (
                    hashlib.sha256(raw).hexdigest() != evidence["sha256"]
                    or json.loads(raw) != material
                    or material["workspace"] != manifest["orchestrator_root"]
                    or material["initial_head"] != manifest["initial_head"]
                    or any(
                        _material_record(
                            _current_material(Path(material["workspace"]), path)
                        )
                        != expected
                        for path, expected in material["after"].items()
                    )
                ):
                    raise SupervisorRefused("WAVE_EVIDENCE_CHANGED")
                integration = {**evidence, "material": material}
            actual_after = {
                path: _material_record(_current_material(Path(manifest["orchestrator_root"]), path))
                for path in material_intent["expected_after"]
            }
            if (_head(Path(manifest["orchestrator_root"])) != manifest["initial_head"]
                    or actual_after != material_intent["expected_after"]):
                quarantine(self.store, self.token, wave_key=prefix, reason="post-apply-material-changed")
                raise SupervisorRefused("WAVE_INTEGRATION_RECONCILIATION_REQUIRED")
            from .wave_execution import capture_integration_candidate
            candidate_output = capture_integration_candidate(
                self.store, self.token, journal, self.supervisor.evidence_root)
            with self.store.transaction() as tx:
                guard(tx)
                self.store._record_event_once_tx(tx, self.token, parent, prefix + ':candidate-output', candidate_output)
                integration_authority.mark_integration_applied_tx(
                    tx,
                    self.token,
                    journal,
                    actual_after,
                )
                self.store._record_event_once_tx(
                    tx,
                    self.token,
                    parent,
                    prefix + ":integrated",
                    {
                        "event_id": event_id,
                        "evidence": {
                            "locator": integration["locator"],
                            "sha256": integration["sha256"],
                        },
                        "material": integration["material"],
                    },
                )
            name = prefix.replace(":", "-") + "-reply.json"
            try:
                evidence = _publish(self.supervisor.evidence_root, name, reply)
            except FileExistsError:
                path = self.supervisor.evidence_root / name
                raw = _read_evidence(path)
                if raw != _canonical(reply) + b"\n":
                    raise SupervisorRefused("WAVE_EVIDENCE_CHANGED") from None
                evidence = {
                    "locator": str(path),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            with self.store.transaction() as tx:
                guard(tx)
                integration_authority.publish_integration_tx(
                    tx, self.token, journal, integration["material"]["after"]
                )
                self.store._record_event_once_tx(
                    tx,
                    self.token,
                    parent,
                    prefix + ":reply",
                    {"reply": reply, "evidence": evidence},
                )
        return reply
