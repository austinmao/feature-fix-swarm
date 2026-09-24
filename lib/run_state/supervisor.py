"""Supervisor-owned process dispatch using the existing ControlStore authority.

The process transport is usable by host adapters; it is not proof that a host's
native tools enforce admission or sandbox boundaries. Host qualification stays
a separate, mandatory consumer gate.
"""
from __future__ import annotations

from .run_policy import productive_work

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

from process_identity import DEAD, LIVE, ProcessIdentity, probe_identity
from run_context import git_admin_lock, resolve_repository, sanitized_git_environment
from host_capabilities import (
    ARTIFACT_REVIEW_CONTEXT_LIMIT, ArtifactReviewMaterial, CapabilityError,
    validate_artifact_review_material,
)
from .codex_host import CodexLaunchMaterial, TelemetryRefused, parse_codex_telemetry
from .claude_host import (
    ClaudeLaunchMaterial, ClaudeHostRefused, ClaudeTelemetryRefused,
    parse_claude_telemetry,
)
from .ownership import OwnershipRefused
from .native_review_transport import NativeReviewLaunchMaterial
from .state import ControlStoreRefused
from .local_check_runtime import (
    LocalCheckMaterial, LocalCheckRefused, sealed_check_material, validate_local_check_material,
)
from .workspace import (
    _from_row, _open_directory_chain_raw, _verify_snapshot_complete,
    _read_anchored_regular_metadata, WorkspaceRefused,
    begin_child_workspace_preparation, finalize_ready_unlock, inspect_ready_layout, inspect_workspace,
    load_input_snapshot, prepare_workspace, recover_workspace_preparation, finalization_apply,
    finalization_preview,
)


class SupervisorRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def artifact_review_inputs(store, preparation, selected_artifacts) -> dict[str, dict]:
    """Return immutable selected input bytes and retained capture descriptors.

    This is a pure verification projection.  It creates no authority, and the
    caller still decides whether these inputs are used to rebuild material.
    """
    _verify_snapshot_complete(store, preparation, verify_workspace=False)
    snapshot = load_input_snapshot(store, preparation)
    if snapshot is None:
        raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
    entries = {entry.path: entry for entry in snapshot.selection.entries}
    required = {item.path for item in snapshot.selection.required_context}
    requested = dict(selected_artifacts)
    if not requested:
        raise WorkspaceRefused("SELECTION_INPUT_MISSING")
    # Required context has no implicit base/primary fallback. A required entry
    # participates only when the caller explicitly selected its copied capture.
    for path in required:
        entry = entries.get(path)
        if entry is None or entry.operation != "copy" or path not in requested:
            raise WorkspaceRefused("SELECTION_INPUT_MISSING")
    contents, evidence = {}, {}
    remaining = 32 * 1024
    capture_files = snapshot.staging / "files"
    for path, expected_digest in sorted(requested.items()):
        entry = entries.get(path)
        if entry is None or entry.operation != "copy" or entry.sha256 != expected_digest:
            raise WorkspaceRefused("SELECTION_INPUT_MISSING")
        data, _metadata = _read_anchored_regular_metadata(
            capture_files, path, max_bytes=remaining,
        )
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise WorkspaceRefused("SNAPSHOT_INCOMPLETE")
        try:
            contents[path] = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceRefused("INVALID_SELECTION") from error
        evidence[path] = {
            "locator": str(capture_files / path), "sha256": expected_digest,
        }
        remaining -= len(data)
    # Recheck capture membership after every anchored read. The descriptor can
    # retain old bytes if a leaf is replaced during the capture walk.
    _verify_snapshot_complete(store, preparation, verify_workspace=False)
    return {
        "contents": contents,
        "provenance": {
            "input_digest": preparation.input_digest,
            "preparation_id": preparation.id,
            "repository_id": preparation.repository_id,
            "run_id": preparation.run_id,
            "selection_manifest_hash": preparation.selected_manifest_hash,
        },
        "evidence": evidence,
    }


def _workspace_identity(path: Path) -> list[int]:
    directory = _open_directory_chain_raw(Path(path.anchor), path.parts[1:], create=False)
    try:
        info = os.fstat(directory)
        return [info.st_dev, info.st_ino]
    finally:
        os.close(directory)


_SUPERVISOR_FRAME_LIMIT = 64 * 1024
_NATIVE_REVIEW_BOOTSTRAP_LIMIT = (2 * ARTIFACT_REVIEW_CONTEXT_LIMIT) + (16 * 1024)


def _bootstrap_frame_limit(*, native_review: bool) -> int:
    """Return the fixed ceiling for the initial inherited-channel message."""
    return _NATIVE_REVIEW_BOOTSTRAP_LIMIT if native_review else _SUPERVISOR_FRAME_LIMIT


def _send(channel: socket.socket, value: dict, *, max_bytes: int = _SUPERVISOR_FRAME_LIMIT) -> None:
    raw = _canonical(value)
    if len(raw) > max_bytes:
        raise SupervisorRefused("MESSAGE_TOO_LARGE")
    channel.sendall(len(raw).to_bytes(4, "big") + raw)


def _receive(channel: socket.socket, *, max_bytes: int = _SUPERVISOR_FRAME_LIMIT) -> dict:
    def exact(size: int) -> bytes:
        value = b""
        while len(value) < size:
            chunk = channel.recv(size - len(value))
            if not chunk:
                raise SupervisorRefused("SUPERVISOR_CHANNEL_CLOSED")
            value += chunk
        return value

    size = int.from_bytes(exact(4), "big")
    if not 0 < size <= max_bytes:
        raise SupervisorRefused("INVALID_MESSAGE")
    try:
        value = json.loads(exact(size))
    except (ValueError, UnicodeError) as error:
        raise SupervisorRefused("INVALID_MESSAGE") from error
    if not isinstance(value, dict):
        raise SupervisorRefused("INVALID_MESSAGE")
    return value


def _head(workspace: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"], cwd=workspace,
        env=sanitized_git_environment(), capture_output=True, text=True,
        timeout=15, check=True,
    )
    return result.stdout.strip()


def _publish(root: Path, name: str, value: dict) -> dict:
    """Publish one complete receipt without replacing an existing final name."""
    raw = _canonical(value) + b"\n"
    path = root / name
    directory = _open_directory_chain_raw(root, (), create=False)
    stage = "." + name + "." + os.urandom(16).hex() + ".tmp"
    try:
        if os.fstat(directory).st_uid != os.getuid():
            raise SupervisorRefused("EVIDENCE_ROOT_UNSAFE")
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SupervisorRefused("EVIDENCE_CHANGED")
        # link(2) is a same-directory, no-replace final-name publication.
        # If this process dies before unlinking the stage, complete bytes are
        # retained but the nlink-2 receipt remains uncertain until reconciliation.
        os.link(stage, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        staged = os.stat(stage, dir_fd=directory, follow_symlinks=False)
        final = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(final.st_mode) or staged.st_nlink != 2 or final.st_nlink != 2
                or (staged.st_dev, staged.st_ino) != (final.st_dev, final.st_ino)
                or (final.st_dev, final.st_ino) != (info.st_dev, info.st_ino)):
            raise SupervisorRefused("EVIDENCE_CHANGED")
        os.fsync(directory)
        os.unlink(stage, dir_fd=directory)
        os.fsync(directory)
    except BaseException:
        # Staged or conflicting bytes are interruption evidence; never rewrite.
        raise
    finally:
        os.close(directory)
    return {"locator": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _read_evidence(path: Path, expected_identity: tuple[int, int] | None = None) -> bytes:
    """Read one supervisor receipt or stream through a nofollow directory fd."""
    try:
        directory = _open_directory_chain_raw(path.parent, (), create=False)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity)):
                raise SupervisorRefused("EVIDENCE_CHANGED")
            return stream.read()
    except OSError as error:
        raise SupervisorRefused("EVIDENCE_CHANGED") from error


@dataclass(frozen=True)
class DispatchRequest:
    activity_id: str
    request_key: str
    command: tuple[str, ...]
    workspace: str
    expected_head: str
    runtime_identity: str
    token_reservation: int = 0
    contract_hash: str = ""
    host_material: ArtifactReviewMaterial | None = None
    codex_material: CodexLaunchMaterial | None = None
    claude_material: ClaudeLaunchMaterial | None = None
    qualification_material: "QualificationLaunchMaterial | None" = None
    claude_qualification_material: "ClaudeQualificationLaunchMaterial | None" = None
    runtime_receipt_sha256: str | None = None
    local_check_receipt_sha256: str | None = None
    managed_input_sha256: str | None = None
    local_check_material: LocalCheckMaterial | None = None
    # Off by default: generic dispatch retains its direct-child process model.
    # This explicit fixture transport has no native-host qualification claim.
    monitor_result: bool = False
    policy_action_id: str | None = None
    native_review_material: NativeReviewLaunchMaterial | None = None


@dataclass(frozen=True)
class QualificationLaunchMaterial:
    """One fixed observer probe before a runtime receipt exists."""

    probe_name: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: str
    contract_sha256: str
    envelope_sha256: str
    runtime_home: str
    runtime_template_sha256: str

    def execution_environment(self) -> dict[str, str]:
        return dict(self.environment)


@dataclass(frozen=True)
class ClaudeQualificationLaunchMaterial:
    """Immutable Claude probe material, run only through ``launch_qualification``."""
    probe_name: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: str
    contract_sha256: str
    envelope_sha256: str
    runtime_home: str
    runtime_template_sha256: str
    model: str
    version: str
    session_id: str | None
    credential_path: str | None
    credential_sha256: str | None
    credential_device: int | None
    credential_inode: int | None

    def execution_environment(self) -> dict[str, str]:
        return dict(self.environment)


@dataclass
class ProcessHandle:
    process: subprocess.Popen | None
    intent_id: str
    activity_id: str
    identity: ProcessIdentity
    initial_head: str
    started_at: float
    stdout_path: Path
    stderr_path: Path
    stream_identities: dict
    result: dict | None = None
    recorded: bool = False
    monitor_identity: ProcessIdentity | None = None
    monitored: bool = False
    replay_material: dict | None = None
    codex_material: CodexLaunchMaterial | None = None
    claude_material: ClaudeLaunchMaterial | None = None
    qualification_material: QualificationLaunchMaterial | None = None
    claude_qualification_material: ClaudeQualificationLaunchMaterial | None = None
    local_check_material: LocalCheckMaterial | None = None


@dataclass
class _WaitingCohortChild:
    request: DispatchRequest
    preparation: object
    workspace_identity: list[int]
    process: subprocess.Popen
    channel: socket.socket
    intent_id: str
    identity: ProcessIdentity
    stdout_path: Path
    stderr_path: Path
    stream_identities: dict[str, tuple[int, int]]
    monitor_identity: ProcessIdentity | None = None


class Supervisor:
    """One live owner; only this process holds the fence and writes authority."""

    @staticmethod
    def observe_finalization(store, repository_id: str, run_id: str, preparation_id: str) -> dict:
        """Observe existing metadata without acquiring or reviving an owner."""
        return finalization_preview(store, repository_id, run_id, preparation_id)

    @staticmethod
    def apply_finalization(store, repository_id: str, run_id: str, preparation_id: str,
                           *, expected_generation: int,
                           expected_manifest_sha256: str) -> dict:
        """Apply an explicitly preview-fenced, evidence-first finalization."""
        return finalization_apply(
            store, repository_id, run_id, preparation_id,
            expected_generation=expected_generation,
            expected_manifest_sha256=expected_manifest_sha256,
        )

    def __init__(self, store, token, *, evidence_root: Path, fault_probe=None, worker_channel=None,
                 shared_resource_coordinator=None, resource_demand_policy=None):
        self.store = store
        self.token = token
        self.evidence_root = Path(evidence_root)
        self.fault_probe = fault_probe
        if worker_channel is not None and (
                worker_channel.token != token or worker_channel.store.db_path != store.db_path):
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        self.worker_channel = worker_channel
        if self.worker_channel is not None:
            self.worker_channel.attach_delegate_consumer(self.consume_delegate_request)
        self._dispatch_lock = threading.RLock()
        self._handles: dict[str, ProcessHandle] = {}
        self._local_check_commands: dict[str, tuple[str, ...]] = {}
        self.shared_resource_coordinator = shared_resource_coordinator
        self.resource_demand_policy = resource_demand_policy
        self._shared_reservations = {}

    def _initialize_shared_resources(self):
        if self.shared_resource_coordinator is not None:
            return
        with self.store.read_transaction() as tx:
            row = tx.execute("SELECT writer_version FROM context_runs WHERE repository_id=? AND run_id=?",
                             (self.token.repository_id, self.token.run_id)).fetchone()
        if row is not None and row["writer_version"] == "ffs-supervisor/1":
            from .shared_resources import SharedResourceCoordinator, cold_start_demand
            self.shared_resource_coordinator = SharedResourceCoordinator(self.store, self.token)
            self.resource_demand_policy = self.resource_demand_policy or cold_start_demand

    def _reserve_shared_resources(self, requests, *, group_key=None):
        self._initialize_shared_resources()
        if self.shared_resource_coordinator is None:
            return ()
        if not callable(self.resource_demand_policy):
            raise SupervisorRefused("RESOURCE_DEMAND_POLICY_REQUIRED")
        return self.shared_resource_coordinator.acquire(tuple({
            "activity_id": request.activity_id, "request_key": request.request_key,
            "material": self._dispatch_material(request),
            "demand": self.resource_demand_policy(request),
        } for request in requests), group_key=group_key)

    def configure_managed_parent_resources(self, request, context, preparation, upstream_runtime):
        from .prelaunch_inventory import freeze_managed_plan_inventory, PrelaunchInventoryRefused
        from .managed_resource_group import ManagedParentResourceCoordinator, settle_predecessor_groups
        self._initialize_shared_resources()
        if self.shared_resource_coordinator is None:
            raise SupervisorRefused('RESOURCE_PARENT_GROUP_REQUIRED')
        # A successor never adopts a predecessor's prepaid tickets: proven-settled old groups close here,
        # unproven ones stay reserved (conservative), then this generation reserves its own group.
        settle_predecessor_groups(self.store, self.token, self.shared_resource_coordinator.queue)
        try:
            with productive_work(self.store, self.token, kind='preparation'):
                inventory, digest = freeze_managed_plan_inventory(self.store, self.token, context, preparation,
                    activity_id=request.activity_id, runtime_identity=request.runtime_identity,
                    runtime=upstream_runtime, evidence_root=self.evidence_root, request_key=request.request_key)
            self.shared_resource_coordinator = ManagedParentResourceCoordinator(self.shared_resource_coordinator,
                parent_request=request, inventory=inventory, inventory_hash=digest,
                demand=self.resource_demand_policy(replace(request, monitor_result=True)))
        except PrelaunchInventoryRefused as error:
            raise SupervisorRefused(str(error)) from error

    def _bind_shared_resource(self, intent_id, reservation=None, *, consumer=None):
        if self.shared_resource_coordinator is None:
            return
        reservation = reservation or self._shared_reservations.get(intent_id)
        if reservation is None:
            raise SupervisorRefused("SHARED_RESOURCE_LEASE_REQUIRED")
        self.shared_resource_coordinator.bind_intent(reservation, intent_id, consumer=consumer)
        self._shared_reservations[intent_id] = reservation

    def _assert_shared_spawn_safe(self, intent_id):
        from .managed_resource_group import ManagedParentResourceCoordinator
        coordinator = self.shared_resource_coordinator
        if not isinstance(coordinator, ManagedParentResourceCoordinator):
            return
        try:
            coordinator.assert_spawn_safe(self._shared_reservations[intent_id])
        except ControlStoreRefused as error:
            parent = self.store.get_activity(coordinator.parent_activity_id)
            if parent.state not in {'succeeded', 'failed', 'aborted'}:
                self.store.transition_activity(self.token, parent.id, expected=parent.state, new='failed',
                    reason='reserved group cannot safely spawn: ' + error.code)
            self.contain_revoked()
            raise SupervisorRefused(error.code) from error

    @staticmethod
    def _policy_clock() -> dict:
        # ControlStore samples after acquiring its writer transaction.
        return {"clock_boot_id": None, "clock_monotonic_ns": None}

    def reserve_request_action(self, request: DispatchRequest, *, action: str,
                               qualification_contract: dict | None = None,
                               recovery_cycle: int | None = None) -> DispatchRequest:
        """Bind a trusted producer's action to its exact dispatch input.

        ``recovery_cycle`` binds a diagnosis or trial reservation to its issued
        cycle ordinal; the ControlStore refuses a trial without one.
        """
        budget = self.store.get_run_policy_budget(
            repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        if budget is None:
            return request
        material = self._dispatch_material(request)
        digest = hashlib.sha256(_canonical(material)).hexdigest()
        if action == "qualification":
            probe = request.qualification_material or request.claude_qualification_material
            if probe is None:
                raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
            # Native transport material and authority probe contracts are
            # separately validated closures, not interchangeable digests.
            contract, _, digest, envelope_hash, _ = self.store._qualification_contract_material(
                qualification_contract, request_key=request.request_key,
                managed_input_sha256=request.managed_input_sha256,
            )
            authority_probe = contract["probe_contract"]
            if (envelope_hash != probe.envelope_sha256
                    or authority_probe["command_sha256"] != hashlib.sha256(_canonical(probe.argv)).hexdigest()
                    or authority_probe["environment_sha256"] != hashlib.sha256(_canonical(probe.environment)).hexdigest()):
                raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        if request.policy_action_id is not None:
            from .run_policy import action_can_mutate
            with self.store.read_transaction() as tx:
                row = tx.execute(
                    "SELECT * FROM authority_policy_actions WHERE id=? AND repository_id=? AND run_id=?",
                    (request.policy_action_id, self.token.repository_id, self.token.run_id),
                ).fetchone()
            if row is None or (row["action"], row["logical_key"], row["input_hash"], bool(row["mutation_allowed"])) != (
                action, request.request_key, digest, action_can_mutate(action),
            ):
                raise SupervisorRefused("POLICY_ACTION_BINDING_CONFLICT")
            return request
        reservation = self.store.reserve_policy_action(
            self.token, action=action, logical_key=request.request_key,
            input_hash=digest, recovery_cycle=recovery_cycle,
        )
        return replace(request, policy_action_id=reservation.id)

    @staticmethod
    def _delegate_key(event_id: int) -> str:
        return f"delegate-allocation:{event_id}"

    def _assert_delegate_request_peer(
        self, intent_id: str, registered: dict, peer_value: object,
    ) -> ProcessIdentity:
        """Revalidate the exact primary or its one approved broker transport."""
        try:
            peer = ProcessIdentity(**peer_value)
        except (TypeError, ValueError) as error:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH") from error
        if asdict(peer) != peer_value:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        if peer_value == registered.get("identity"):
            if probe_identity(peer) != LIVE:
                raise SupervisorRefused("IPC_PEER_UNKNOWN")
            return peer
        if self.worker_channel is None:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        # Local import avoids the module-level cycle: worker_channel refers to
        # Supervisor only from its server error boundary.
        from .worker_channel import WorkerChannelRefused
        try:
            self.worker_channel.assert_authorized_peer(intent_id, peer)
        except WorkerChannelRefused as error:
            raise SupervisorRefused(error.code) from error
        return peer

    def _delegate_admission_guard(
        self, event_id: int, intent_id: str, parent_id: str, body: dict,
        registered: dict, parent_preparation_id: str, parent_native_identity: str,
        request_peer_identity: dict,
    ):
        """Return the final authority recheck for one retained delegate event.

        Filesystem probes happen before this closure is constructed.  Every
        mutation calls the closure again in its own fenced transaction, where
        it ties the captured request and registration receipts to the current
        intent, process incarnation, parent workspace, and full ancestry.
        """
        expected_identity = registered["identity"]
        self._assert_delegate_request_peer(intent_id, registered, request_peer_identity)
        try:
            expected_workspace_identity = json.loads(parent_native_identity)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH") from error
        expected_request = {
            "operation": "delegate-request", "body": body, "intent_id": intent_id,
            "peer_identity": request_peer_identity, "workspace": registered["workspace"],
            "runtime_identity": body["runtime_identity"],
        }
        request_payload_hash = hashlib.sha256(_canonical(expected_request)).hexdigest()
        registration_payload_hash = hashlib.sha256(_canonical(registered)).hexdigest()

        def guard(tx):
            self._assert_delegate_request_peer(intent_id, registered, request_peer_identity)
            self.store._assert_activity_binding(tx, self.token, parent_id)
            self.store._assert_activity_ancestry(
                tx, parent_id, repository_id=self.token.repository_id, run_id=self.token.run_id,
            )
            request = tx.execute(
                "SELECT k.idempotency_key,k.payload_hash,e.event_type,e.payload FROM authority_event_keys k "
                "JOIN control_events e ON e.id=k.event_id WHERE k.activity_id=? AND k.event_id=?",
                (parent_id, event_id),
            ).fetchone()
            if (request is None or request["idempotency_key"] != request["event_type"]
                    or request["payload_hash"] != request_payload_hash
                    or not request["event_type"].startswith("worker-request:" + intent_id + ":")):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            try:
                request_payload = json.loads(request["payload"])
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise SupervisorRefused("IPC_SCOPE_MISMATCH") from error
            if request_payload != {
                "run_id": self.token.run_id, "activity_id": parent_id, "data": expected_request,
            }:
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            registration = tx.execute(
                "SELECT k.idempotency_key,k.payload_hash,e.payload FROM authority_event_keys k "
                "JOIN control_events e ON e.id=k.event_id "
                "WHERE k.activity_id=? AND k.idempotency_key=?",
                (parent_id, "worker-registration:" + intent_id),
            ).fetchone()
            try:
                registration_payload = json.loads(registration["payload"])
            except (TypeError, ValueError, json.JSONDecodeError, KeyError) as error:
                raise SupervisorRefused("IPC_SCOPE_MISMATCH") from error
            if (registration["idempotency_key"] != "worker-registration:" + intent_id
                    or registration["payload_hash"] != registration_payload_hash
                    or registration_payload != {
                "run_id": self.token.run_id, "activity_id": parent_id, "data": registered,
            }):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            row = tx.execute(
                "SELECT i.generation,i.state,i.child_host_id,i.child_boot_id,i.child_pid,i.child_start_token,"
                "a.state AS activity_state,a.runtime_tuple_hash,b.candidate_hash,b.contract_hash,"
                "b.runtime_identity,b.workspace_binding,b.workspace_preparation_id FROM authority_launch_intents i "
                "JOIN authority_activities a ON a.id=i.activity_id "
                "JOIN authority_child_bindings b ON b.activity_id=a.id WHERE i.id=? AND a.id=?",
                (intent_id, parent_id),
            ).fetchone()
            if (row is None or row["generation"] != self.token.generation
                    or row["state"] != "released_to_execute" or row["activity_state"] != "active"
                    or row["runtime_tuple_hash"] != body["runtime_identity"]
                    or row["candidate_hash"] != body["candidate_hash"]
                    or row["contract_hash"] != body["contract_hash"]
                    or row["runtime_identity"] != body["runtime_identity"]
                    or row["workspace_binding"] != registered["workspace"]
                    or row["workspace_preparation_id"] != parent_preparation_id
                    or {
                        "host_id": row["child_host_id"], "boot_id": row["child_boot_id"],
                        "pid": row["child_pid"], "start_token": row["child_start_token"],
                    } != expected_identity):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            identity = ProcessIdentity(
                row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"],
            )
            if probe_identity(identity) != LIVE:
                raise SupervisorRefused("IPC_PEER_UNKNOWN")
            workspace = tx.execute(
                "SELECT path,native_identity_json,state,created_by_ffs,generation FROM context_workspaces "
                "WHERE preparation_id=? AND repository_id=? AND run_id=?",
                (parent_preparation_id, self.token.repository_id, self.token.run_id),
            ).fetchone()
            if (workspace is None or workspace["path"] != registered["workspace"]
                    or workspace["native_identity_json"] != parent_native_identity
                    or workspace["state"] != "ready" or not workspace["created_by_ffs"]
                    or workspace["generation"] != self.token.generation):
                raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
            # This is a descriptor-anchored stat, not a Git probe.  It runs
            # under the authority transaction at every effect boundary so a
            # replacement after the initial read cannot inherit the request.
            try:
                if _workspace_identity(Path(registered["workspace"])) != expected_workspace_identity:
                    raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
            except OSError as error:
                raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH") from error
        return guard

    def _read_delegate_request(self, event_id: int):
        """Reconstruct an IPC request from its immutable event and registration."""
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise SupervisorRefused("IPC_SCOPE_MISMATCH")
        with self.store.read_transaction() as tx:
            event = tx.execute("SELECT event_type,payload FROM control_events WHERE id=?", (event_id,)).fetchone()
            if event is None or not isinstance(event["event_type"], str) or not event["event_type"].startswith("worker-request:"):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            try:
                wrapped = json.loads(event["payload"])
                activity_id, data = wrapped["activity_id"], wrapped["data"]
            except (KeyError, TypeError, ValueError) as error:
                raise SupervisorRefused("IPC_SCOPE_MISMATCH") from error
            if (not isinstance(wrapped, dict) or wrapped.get("run_id") != self.token.run_id
                    or not isinstance(activity_id, str) or not isinstance(data, dict)
                    or data.get("operation") != "delegate-request"):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            body = data.get("body")
            required = {"parent_activity_id", "role", "candidate_hash", "contract_hash", "runtime_identity"}
            event_binding = tx.execute(
                "SELECT idempotency_key FROM authority_event_keys WHERE activity_id=? AND event_id=?",
                (activity_id, event_id),
            ).fetchone()
            if (not isinstance(body, dict) or set(body) != required or body["parent_activity_id"] != activity_id
                    or body["role"] not in {"worker", "reviewer", "recovery", "inventory"}
                    or not all(isinstance(body[key], str) and body[key] for key in (
                        "candidate_hash", "contract_hash", "runtime_identity",
                    ))
                    or event_binding is None or event_binding["idempotency_key"] != event["event_type"]):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            # Require the exact registration event for the originating intent,
            # then validate its current authority records below.  A forged or
            # stale peer event cannot bootstrap a successor generation.
            intent_id = data.get("intent_id")
            if (not isinstance(intent_id, str) or not intent_id
                    or not event["event_type"].startswith("worker-request:" + intent_id + ":")):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            registration = tx.execute(
                "SELECT event_id FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                (activity_id, "worker-registration:" + str(intent_id)),
            ).fetchone()
            if registration is None:
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            registration_event = tx.execute(
                "SELECT payload FROM control_events WHERE id=?", (registration["event_id"],),
            ).fetchone()
            try:
                registered = json.loads(registration_event["payload"])["data"]
            except (KeyError, TypeError, ValueError) as error:
                raise SupervisorRefused("IPC_SCOPE_MISMATCH") from error
            intent = tx.execute(
                "SELECT i.*,a.repository_id,a.run_id,a.state AS activity_state,a.runtime_tuple_hash "
                "FROM authority_launch_intents i JOIN authority_activities a ON a.id=i.activity_id WHERE i.id=?",
                (intent_id,),
            ).fetchone()
            binding = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (activity_id,)).fetchone()
            if (intent is None or binding is None or intent["repository_id"] != self.token.repository_id
                    or intent["run_id"] != self.token.run_id or intent["activity_id"] != activity_id
                    or intent["generation"] != self.token.generation or intent["state"] != "released_to_execute"
                    or intent["activity_state"] != "active" or intent["runtime_tuple_hash"] != body["runtime_identity"]
                    or binding["candidate_hash"] != body["candidate_hash"]
                    or binding["contract_hash"] != body["contract_hash"]
                    or binding["runtime_identity"] != body["runtime_identity"]
                    or not isinstance(registered, dict)
                    or registered.get("intent_id") != intent_id
                    or registered.get("activity_id") != activity_id
                    or registered.get("generation") != self.token.generation
                    or registered.get("repository_id") != self.token.repository_id
                    or registered.get("run_id") != self.token.run_id
                    or registered.get("workspace") != binding["workspace_binding"]
                    or registered.get("candidate_hash") != body["candidate_hash"]
                    or registered.get("contract_hash") != body["contract_hash"]
                    or registered.get("runtime_identity") != body["runtime_identity"]
                    or body["role"] not in registered.get("allowed_roles", [])
                    or registered.get("identity") != {
                        "host_id": intent["child_host_id"], "boot_id": intent["child_boot_id"],
                        "pid": intent["child_pid"], "start_token": intent["child_start_token"],
                    }):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            identity = ProcessIdentity(intent["child_host_id"], intent["child_boot_id"],
                                       intent["child_pid"], intent["child_start_token"])
            if probe_identity(identity) != LIVE:
                raise SupervisorRefused("IPC_PEER_UNKNOWN")
            request_peer_identity = data.get("peer_identity")
            self._assert_delegate_request_peer(intent_id, registered, request_peer_identity)
            if (data.get("workspace") != registered.get("workspace")
                    or data.get("runtime_identity") != registered.get("runtime_identity")):
                raise SupervisorRefused("IPC_SCOPE_MISMATCH")
            self.store._assert_activity_binding(tx, self.token, activity_id)
            self.store._assert_activity_ancestry(tx, activity_id, repository_id=self.token.repository_id, run_id=self.token.run_id)
            parent_workspace = tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id=? AND repository_id=? AND run_id=? "
                "AND generation=? AND state='ready' AND created_by_ffs=1",
                (binding["workspace_preparation_id"], self.token.repository_id, self.token.run_id, self.token.generation),
            ).fetchone()
            if parent_workspace is None or parent_workspace["path"] != binding["workspace_binding"]:
                raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
            from run_state.workspace import _assert_preparation_binding
            _assert_preparation_binding(parent_workspace, self.token, require_generation=True, tx=tx)
            try:
                parent_native_identity = json.loads(parent_workspace["native_identity_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH") from error
            if (not isinstance(parent_native_identity, list) or len(parent_native_identity) != 2
                    or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                           for value in parent_native_identity)):
                raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        preparation = inspect_workspace(self.store, binding["workspace_preparation_id"])
        if (preparation.path != Path(binding["workspace_binding"])
                or _workspace_identity(preparation.path) != parent_native_identity):
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        # The selection manifest alone is not authority.  Verify the retained
        # capture and its completion receipt before copying any selected bytes.
        _verify_snapshot_complete(self.store, preparation, verify_workspace=False)
        snapshot = load_input_snapshot(self.store, preparation)
        if snapshot is None or snapshot.input_digest != body["candidate_hash"]:
            raise SupervisorRefused("INPUT_SELECTION_CHANGED")
        origin_manifest = snapshot.manifest
        origin_manifest_json = json.dumps(origin_manifest, sort_keys=True, separators=(",", ":"))
        capture = origin_manifest.get("capture")
        if (
            not isinstance(capture, dict)
            or not isinstance(capture.get("locator"), str) or not capture["locator"]
            or not isinstance(capture.get("files_hash"), str) or len(capture["files_hash"]) != 64
        ):
            raise SupervisorRefused("INPUT_SELECTION_CHANGED")
        return (
            activity_id, body, preparation, snapshot, origin_manifest_json,
            self._delegate_admission_guard(
                event_id, intent_id, activity_id, body, registered,
                binding["workspace_preparation_id"], parent_workspace["native_identity_json"],
                request_peer_identity,
            ),
        )

    def _validate_delegate_replay_tx(
        self, tx, *, event_id: int, parent_id: str, body: dict, key: str, origin_manifest_json: str,
        admission_guard, allocation: dict | None = None,
    ) -> dict:
        """Validate a receipt without treating its presence as current authority."""
        admission_guard(tx)
        if allocation is None:
            receipt = tx.execute(
                "SELECT k.idempotency_key,k.payload_hash,e.event_type,e.payload "
                "FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                "WHERE k.activity_id=? AND k.idempotency_key=?",
                (parent_id, key),
            ).fetchone()
            try:
                wrapped = json.loads(receipt["payload"])
                allocation = wrapped["data"]
            except (TypeError, ValueError, json.JSONDecodeError, KeyError) as error:
                raise SupervisorRefused("REQUEST_BINDING_UNKNOWN") from error
            if (receipt["idempotency_key"] != key or receipt["event_type"] != key
                    or receipt["payload_hash"] != hashlib.sha256(_canonical(allocation)).hexdigest()
                    or wrapped != {"run_id": self.token.run_id, "activity_id": parent_id, "data": allocation}):
                raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
        expected_fields = {
            "status", "event_id", "activity_id", "workspace", "workspace_preparation_id",
        }
        if (not isinstance(allocation, dict) or set(allocation) != expected_fields
                or allocation["status"] != "registered_allocation" or allocation["event_id"] != event_id
                or not all(isinstance(allocation[field], str) and allocation[field]
                           for field in ("activity_id", "workspace", "workspace_preparation_id"))):
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
        child = tx.execute(
            "SELECT a.repository_id,a.run_id,a.generation,a.request_key,a.input_digest,a.runtime_tuple_hash,"
            "b.parent_activity_id,b.role,b.candidate_hash,b.contract_hash,b.runtime_identity,"
            "b.workspace_binding,b.workspace_preparation_id,w.path,w.state,w.created_by_ffs,w.generation AS workspace_generation,"
            "w.parent_preparation_id,w.parent_activity_id AS workspace_parent_activity_id,w.child_role,w.child_request_key,"
            "w.native_identity_json,w.selected_manifest_json,w.selected_manifest_hash,"
            "s.input_digest AS snapshot_input_digest,s.full_manifest_hash,s.capture_locator,s.capture_hash,s.completion_hash "
            "FROM authority_activities a JOIN authority_child_bindings b ON b.activity_id=a.id "
            "JOIN context_workspaces w ON w.preparation_id=b.workspace_preparation_id "
            "JOIN context_input_snapshots s ON s.preparation_id=w.preparation_id WHERE a.id=?",
            (allocation["activity_id"],),
        ).fetchone()
        origin_manifest_hash = hashlib.sha256(origin_manifest_json.encode()).hexdigest()
        try:
            origin_manifest = json.loads(origin_manifest_json)
            origin_capture = origin_manifest["capture"]
        except (TypeError, ValueError, json.JSONDecodeError, KeyError) as error:
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN") from error
        if (
            child is None or child["repository_id"] != self.token.repository_id
            or child["run_id"] != self.token.run_id or child["generation"] != self.token.generation
            or child["request_key"] != key or child["input_digest"] != body["candidate_hash"]
            or child["runtime_tuple_hash"] != body["runtime_identity"]
            or child["parent_activity_id"] != parent_id or child["role"] != body["role"]
            or child["candidate_hash"] != body["candidate_hash"]
            or child["contract_hash"] != body["contract_hash"]
            or child["runtime_identity"] != body["runtime_identity"]
            or child["workspace_preparation_id"] != allocation["workspace_preparation_id"]
            or child["workspace_binding"] != allocation["workspace"]
            or child["path"] != allocation["workspace"] or child["state"] != "ready"
            or not child["created_by_ffs"] or child["workspace_generation"] != self.token.generation
            or child["workspace_parent_activity_id"] != parent_id or child["child_role"] != body["role"]
            or child["child_request_key"] != key or child["snapshot_input_digest"] != body["candidate_hash"]
            or child["selected_manifest_json"] != origin_manifest_json
            or child["selected_manifest_hash"] != origin_manifest.get("selection_manifest_hash")
            or child["full_manifest_hash"] != origin_manifest_hash
            or child["capture_locator"] != origin_capture.get("locator")
            or child["capture_hash"] != origin_capture.get("files_hash")
            or not isinstance(child["completion_hash"], str) or len(child["completion_hash"]) != 64
        ):
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
        from run_state.workspace import _assert_preparation_binding
        workspace = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id=?",
            (allocation["workspace_preparation_id"],),
        ).fetchone()
        try:
            native_identity = json.loads(child["native_identity_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN") from error
        if (not isinstance(native_identity, list) or len(native_identity) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                       for value in native_identity)):
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
        try:
            _assert_preparation_binding(workspace, self.token, require_generation=True, tx=tx)
        except OwnershipRefused as error:
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN") from error
        return allocation

    def consume_delegate_request(self, event_id: int) -> dict:
        """Allocate a child only from a retained, authenticated request event.

        This runs after IPC admission.  Each durable mutation is separately
        fenced; filesystem preparation is left to the existing recovery-safe
        workspace protocol.
        """
        parent_id, body, parent_preparation, snapshot, origin_manifest_json, admission_guard = self._read_delegate_request(event_id)
        key = self._delegate_key(event_id)
        # A completed allocation receipt is a stable replay response.
        replay = None
        with self.store.read_transaction() as tx:
            prior = tx.execute(
                "SELECT event_id FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                (parent_id, key),
            ).fetchone()
            if prior is not None:
                replay = self._validate_delegate_replay_tx(
                    tx, event_id=event_id, parent_id=parent_id, body=body, key=key,
                    origin_manifest_json=origin_manifest_json,
                    admission_guard=admission_guard,
                )
        if replay is not None:
            ready = inspect_workspace(self.store, replay["workspace_preparation_id"])
            if ready.path != Path(replay["workspace"]):
                raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
            # A READY allocation may already have executed and edited its
            # selected files. Replay verifies retained inputs and authority;
            # dispatch replay then resolves the existing intent without spawn.
            _verify_snapshot_complete(self.store, ready, verify_workspace=False)
            descriptor = resolve_repository(ready.repository_path)
            with git_admin_lock(descriptor.common_dir):
                with self.store.fenced_operation(self.token):
                    if inspect_ready_layout(ready) != "ready":
                        raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
                    with self.store.transaction() as tx:
                        return self._validate_delegate_replay_tx(
                            tx, event_id=event_id, parent_id=parent_id, body=body, key=key,
                            origin_manifest_json=origin_manifest_json,
                            admission_guard=admission_guard,
                        )
        # The preparation helper performs its own owner-checked transaction;
        # do not nest the store's non-reentrant process mutex around it.
        pending = begin_child_workspace_preparation(
            self.store, self.token, parent_activity_id=parent_id, request_key=key,
            role=body["role"], base_commit=parent_preparation.base_commit,
            selected_input_manifest=snapshot.manifest, repository_path=parent_preparation.repository_path,
            admission_guard=admission_guard,
        )
        if pending.state == "preparing":
            if pending.native_identity is None:
                ready = prepare_workspace(
                    self.store, self.token, pending, input_snapshot=snapshot,
                    admission_guard=admission_guard,
                )
            else:
                # A prior attempt created this exact physical identity.  The
                # existing recovery protocol either proves it ready or leaves
                # its interruption evidence intact; it never allocates anew.
                ready = recover_workspace_preparation(
                    self.store, self.token, pending.id, admission_guard=admission_guard,
                )
                finalize_ready_unlock(
                    self.store, self.token, ready.id, admission_guard=admission_guard,
                )
        elif pending.state == "ready":
            ready = pending
            # A crash after READY but before unlock retains Git's native lock.
            # Replaying this authenticated request may release only this exact
            # workspace, under the same admission guard as its first attempt.
            finalize_ready_unlock(
                self.store, self.token, ready.id, admission_guard=admission_guard,
            )
        else:
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        # Re-read current authority and retained capture after Git work before
        # binding the child activity under the current fence.
        parent_id, body, parent_preparation, snapshot, origin_manifest_json, admission_guard = self._read_delegate_request(event_id)
        if ready.input_digest != body["candidate_hash"]:
            raise SupervisorRefused("INPUT_SELECTION_CHANGED")
        child = self.store.create_child_activity(
            self.token, parent_activity_id=parent_id, role=body["role"], request_key=key,
            candidate_hash=body["candidate_hash"], contract_hash=body["contract_hash"],
            runtime_identity=body["runtime_identity"], workspace_binding=str(ready.path),
            workspace_preparation_id=ready.id,
            admission_guard=admission_guard,
        )
        allocation = {"status": "registered_allocation", "event_id": event_id,
                      "activity_id": child.id, "workspace": str(ready.path),
                      "workspace_preparation_id": ready.id}
        # A child registration is not an allocation receipt.  Recheck the
        # derived worktree after its activity commit, under Git then owner
        # fencing, before publishing the durable result that makes replay
        # possible.
        _verify_snapshot_complete(self.store, ready)
        descriptor = resolve_repository(ready.repository_path)
        with git_admin_lock(descriptor.common_dir):
            with self.store.fenced_operation(self.token):
                if inspect_ready_layout(ready) != "ready":
                    raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
                with self.store.transaction() as tx:
                    allocation = self._validate_delegate_replay_tx(
                        tx, event_id=event_id, parent_id=parent_id, body=body, key=key,
                        origin_manifest_json=origin_manifest_json, admission_guard=admission_guard,
                        allocation=allocation,
                    )
                    return self.store._record_event_once_tx(
                        tx, self.token, parent_id, key, allocation,
                    )["payload"]

    def launch_delegate_request(
        self, event_id: int, *, command: tuple[str, ...], runtime_identity: str,
        token_reservation: int = 0,
    ) -> ProcessHandle:
        """Launch an allocation using a command selected by the supervisor.

        Worker IPC remains allocation-only and cannot supply argv. This generic
        direct-child bridge does not qualify a native host or its model transport.
        """
        _parent, body, _preparation, _snapshot, _manifest, _guard = self._read_delegate_request(event_id)
        if runtime_identity != body["runtime_identity"]:
            raise SupervisorRefused("RUNTIME_DRIFT")
        allocation = self.consume_delegate_request(event_id)
        preparation = inspect_workspace(self.store, allocation["workspace_preparation_id"])
        request = DispatchRequest(
            activity_id=allocation["activity_id"], request_key=f"delegate-launch:{event_id}",
            command=command, workspace=str(preparation.path), expected_head=preparation.base_commit,
            runtime_identity=runtime_identity, token_reservation=token_reservation,
            contract_hash=body["contract_hash"],
        )
        return self.launch(self.reserve_request_action(request, action="execute"))

    def _delegate_launch_guard(self, request: DispatchRequest, preparation):
        """Rebuild authority for delegated children, including direct launch callers.

        Retained bytes are hashed outside write transactions. The transaction
        guard binds their stable file and directory identities and the exact
        snapshot records, as well as the originating live request and allocation.
        """
        key = preparation.child_request_key
        if not isinstance(key, str) or not key.startswith("delegate-allocation:"):
            return None
        try:
            event_id = int(key.removeprefix("delegate-allocation:"))
        except ValueError as error:
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN") from error
        if key != self._delegate_key(event_id) or request.request_key != f"delegate-launch:{event_id}":
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
        if (request.host_material is not None or request.codex_material is not None
                or request.claude_material is not None or request.monitor_result):
            raise SupervisorRefused("DELEGATE_TRANSPORT_UNSUPPORTED")
        parent_id, body, origin, snapshot, manifest_json, admission = self._read_delegate_request(event_id)
        with self.store.read_transaction() as tx:
            allocation = self._validate_delegate_replay_tx(
                tx, event_id=event_id, parent_id=parent_id, body=body, key=key,
                origin_manifest_json=manifest_json, admission_guard=admission,
            )
            retained = {
                item.id: dict(tx.execute(
                    "SELECT * FROM context_input_snapshots WHERE preparation_id=?", (item.id,),
                ).fetchone()) for item in (origin, preparation)
            }
        if (allocation["activity_id"] != request.activity_id
                or allocation["workspace_preparation_id"] != preparation.id
                or allocation["workspace"] != request.workspace
                or request.runtime_identity != body["runtime_identity"]
                or request.contract_hash != body["contract_hash"]):
            raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
        self._verify_physical_workspace(preparation)
        identities = {}
        expected_files = {
            snapshot.staging / "files" / item.path: item.sha256
            for item in snapshot.selection.entries if item.operation == "copy"
        }
        for record in retained.values():
            expected_files[Path(record["completion_locator"])] = record["completion_hash"]

        def file_identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        for path, expected_hash in expected_files.items():
            parent_identity = _workspace_identity(path.parent)
            data, info = _read_anchored_regular_metadata(path.parent, path.name)
            if (hashlib.sha256(data).hexdigest() != expected_hash
                    or _workspace_identity(path.parent) != parent_identity):
                raise SupervisorRefused("INPUT_SELECTION_CHANGED")
            identities[path] = (parent_identity, file_identity(info))
        child_identity = _workspace_identity(preparation.path)
        capture_identity = _workspace_identity(snapshot.staging)

        def guard(tx):
            current = self._validate_delegate_replay_tx(
                tx, event_id=event_id, parent_id=parent_id, body=body, key=key,
                origin_manifest_json=manifest_json, admission_guard=admission,
            )
            if current != allocation or _workspace_identity(preparation.path) != child_identity:
                raise SupervisorRefused("REQUEST_BINDING_UNKNOWN")
            if _workspace_identity(snapshot.staging) != capture_identity:
                raise SupervisorRefused("INPUT_SELECTION_CHANGED")
            for prep_id, expected in retained.items():
                row = tx.execute("SELECT * FROM context_input_snapshots WHERE preparation_id=?", (prep_id,)).fetchone()
                if row is None or dict(row) != expected:
                    raise SupervisorRefused("INPUT_SELECTION_CHANGED")
            for path, (parent_identity, expected_identity) in identities.items():
                fd = _open_directory_chain_raw(Path(path.parent.anchor), path.parent.parts[1:], create=False)
                try:
                    info = os.fstat(fd)
                    if [info.st_dev, info.st_ino] != parent_identity:
                        raise SupervisorRefused("INPUT_SELECTION_CHANGED")
                    observed = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                    if (file_identity(observed) != expected_identity
                            or _workspace_identity(path.parent) != parent_identity):
                        raise SupervisorRefused("INPUT_SELECTION_CHANGED")
                except OSError as error:
                    raise SupervisorRefused("INPUT_SELECTION_CHANGED") from error
                finally:
                    os.close(fd)
        return guard

    def _fault(self, point: str) -> None:
        if self.fault_probe is not None:
            self.fault_probe(point)

    def _validate(self, request: DispatchRequest):
        if (not request.request_key or not request.command
                or not all(isinstance(arg, str) and (arg or request.native_review_material is not None)
                           and "\0" not in arg for arg in request.command)
                or not Path(request.command[0]).is_absolute()
                or type(request.monitor_result) is not bool):
            raise SupervisorRefused("INVALID_DISPATCH")
        if self.worker_channel is not None and (
                len(request.contract_hash) != 64
                or any(char not in "0123456789abcdef" for char in request.contract_hash)):
            raise SupervisorRefused("CONTRACT_IDENTITY_REQUIRED")
        materials = tuple(item for item in (
            request.host_material, request.codex_material, request.claude_material,
            request.qualification_material, request.claude_qualification_material,
            request.local_check_material,
            request.native_review_material,
        ) if item is not None)
        if len(materials) > 1:
            raise SupervisorRefused("HOST_MATERIAL_INVALID")
        if request.native_review_material is not None:
            from .native_review_supervision import validate_request
            validate_request(self, request)
        if request.host_material is not None:
            try:
                validate_artifact_review_material(request.host_material)
            except CapabilityError as error:
                raise SupervisorRefused("HOST_MATERIAL_INVALID") from error
            if self.worker_channel is not None:
                # Tool-free artifact review never receives worker IPC credentials.
                raise SupervisorRefused("HOST_MATERIAL_IPC_FORBIDDEN")
        if request.codex_material is not None:
            material = request.codex_material
            if (tuple(request.command) != material.argv or request.workspace != material.cwd
                    or material.runtime_sha256 == "" or material.runtime.to_dict().get("status") != "admitted"):
                raise SupervisorRefused("HOST_MATERIAL_INVALID")
        if request.claude_material is not None:
            material = request.claude_material
            if (tuple(request.command) != material.argv or request.workspace != material.cwd
                    or not material.runtime_sha256
                    or material.runtime.to_dict().get("status") != "admitted"):
                raise SupervisorRefused("HOST_MATERIAL_INVALID")
        if request.qualification_material is not None:
            self._validate_qualification_material(request)
        if request.claude_qualification_material is not None:
            self._validate_claude_qualification_material(request)
        if request.local_check_material is not None:
            self._validate_local_check_material(request)
        if request.monitor_result and (
                request.host_material is not None or request.qualification_material is not None
                or request.claude_qualification_material is not None):
            # Artifact reviews and qualification probes retain their own transport.
            raise SupervisorRefused("MONITOR_TRANSPORT_UNSUPPORTED")
        workspace = Path(request.workspace)
        if not workspace.is_absolute() or workspace.resolve() != workspace:
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        activity = self.store.get_activity(request.activity_id)
        if activity.runtime_tuple_hash != request.runtime_identity:
            raise SupervisorRefused("RUNTIME_DRIFT")
        with self.store.read_transaction() as tx:
            child_binding = tx.execute(
                "SELECT * FROM authority_child_bindings WHERE activity_id=?",
                (request.activity_id,),
            ).fetchone()
            if child_binding is None or child_binding["workspace_binding"] != str(workspace):
                raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
            if child_binding["contract_hash"] != request.contract_hash:
                raise SupervisorRefused("CONTRACT_IDENTITY_REQUIRED")
            limits = tx.execute(
                "SELECT 1 FROM authority_run_limits WHERE repository_id=? AND run_id=?",
                (self.token.repository_id, self.token.run_id),
            ).fetchone()
            if limits is None:
                raise SupervisorRefused("RUN_LIMITS_REQUIRED")
            # Every dispatched activity must use a ready registered child;
            # the owner workspace is never an implicit execution exception.
            bound = tx.execute(
                "SELECT * FROM context_workspaces WHERE repository_id=? AND run_id=? "
                "AND preparation_id=? AND path=? AND state='ready' AND generation=? "
                "AND created_by_ffs=1",
                (self.token.repository_id, self.token.run_id,
                 child_binding["workspace_preparation_id"], str(workspace), self.token.generation),
            ).fetchone()
            if bound is None or workspace == Path(self.token.workspace):
                raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
            from run_state.workspace import _assert_preparation_binding
            _assert_preparation_binding(bound, self.token, require_generation=True, tx=tx)
            if (
                bound["parent_activity_id"] != child_binding["parent_activity_id"]
                or bound["child_role"] != child_binding["role"]
                or child_binding["runtime_identity"] != request.runtime_identity
            ):
                raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
            if bound["base_commit"] != request.expected_head:
                raise SupervisorRefused("FORK_BASE_MISMATCH")
            preparation = _from_row(bound)
        if _head(workspace) != request.expected_head:
            raise SupervisorRefused("FORK_BASE_MISMATCH")
        self._verify_physical_workspace(preparation)
        return preparation

    def _validate_local_check_material(self, request: DispatchRequest) -> None:
        material = request.local_check_material
        assert material is not None
        if (
            request.monitor_result or self.worker_channel is not None
            or any(item is not None for item in (
                request.host_material, request.codex_material, request.claude_material,
                request.qualification_material, request.claude_qualification_material,
            ))
            or request.local_check_receipt_sha256 is None
            or request.runtime_receipt_sha256 is not None
            or request.contract_hash != material.acceptance_hash
            or self._local_check_commands.get(material.material_sha256) != tuple(request.command)
            or not material.confinement_policy_sha256
            or request.workspace != material.workspace
            or request.expected_head != material.expected_head
            or request.runtime_identity != material.runtime_identity
        ):
            raise SupervisorRefused("LOCAL_CHECK_MATERIAL_INVALID")
        sealed = self.store.get_sealed_acceptance(
            repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        try:
            validate_local_check_material(material, sealed=sealed, require_current_bytes=True)
            from .local_check_runtime import verify_local_candidate
            verify_local_candidate(self.store, material)
        except LocalCheckRefused as error:
            raise SupervisorRefused(error.code) from error

    @staticmethod
    def _validate_qualification_material(request: DispatchRequest) -> None:
        material = request.qualification_material
        assert material is not None
        if (type(material) is not QualificationLaunchMaterial
                or material.probe_name not in {
                    "ordinary", "native-positive", "native-negative", "native-multi-agent",
                }
                or tuple(request.command) != material.argv
                or request.workspace != material.cwd
                or request.runtime_identity != material.envelope_sha256
                or request.contract_hash != material.envelope_sha256):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
               for value in (material.contract_sha256, material.envelope_sha256,
                             material.runtime_template_sha256)):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        environment = dict(material.environment)
        if len(environment) != len(material.environment):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        required = {
            "HOME", "CODEX_HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "NO_COLOR",
            "GSD_DISPATCH_MODE", "FFS_SUPERVISED_COMMIT_MODE",
            "FFS_SUPERVISED_ADMISSION_FILE", "FFS_SUPERVISED_DISPATCH_COMMAND_JSON",
            "FFS_HOOK_OBSERVATION", "FFS_HOOK_NONCE",
        }
        if (set(environment) != required
                or any(not isinstance(key, str) or not isinstance(value, str) or "\0" in value
                       for key, value in environment.items())
                or environment["HOME"] != material.runtime_home
                or environment["CODEX_HOME"] != material.runtime_home):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        home, cwd, tmpdir = Path(material.runtime_home), Path(material.cwd), Path(environment["TMPDIR"])
        if (not home.is_absolute() or home.is_symlink() or not home.is_dir()
                or not cwd.is_absolute() or cwd.resolve() != cwd
                or not tmpdir.is_absolute() or tmpdir.resolve().parent != cwd
                or Path(environment["FFS_HOOK_OBSERVATION"]).parent != home):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        from host_capabilities import validate_gsd_supervisor_environment, CapabilityError
        try:
            validate_gsd_supervisor_environment({key: environment[key] for key in (
                "GSD_DISPATCH_MODE", "FFS_SUPERVISED_COMMIT_MODE",
                "FFS_SUPERVISED_ADMISSION_FILE", "FFS_SUPERVISED_DISPATCH_COMMAND_JSON",
            )})
        except CapabilityError as error:
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID") from error
        contract = {
            "schema": "ffs.codex-qualification-probe/v1",
            "probe_name": material.probe_name,
            "argv_sha256": hashlib.sha256(_canonical(material.argv)).hexdigest(),
            "environment_sha256": hashlib.sha256(_canonical(material.environment)).hexdigest(),
            "cwd": material.cwd, "runtime_home": material.runtime_home,
            "runtime_template_sha256": material.runtime_template_sha256,
        }
        if hashlib.sha256(_canonical(contract)).hexdigest() != material.contract_sha256:
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")

    @staticmethod
    def _validate_claude_qualification_material(request: DispatchRequest) -> None:
        material = request.claude_qualification_material
        assert material is not None
        if (
            type(material) is not ClaudeQualificationLaunchMaterial
            or material.probe_name not in {"auth-negative", "session-model", "sandbox-hooks", "nested-auth"}
            or tuple(request.command) != material.argv or request.workspace != material.cwd
            or request.runtime_identity != material.envelope_sha256
            or request.contract_hash != material.envelope_sha256
            or any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
                   for value in (material.contract_sha256, material.envelope_sha256,
                                 material.runtime_template_sha256))
            or not isinstance(material.model, str) or not material.model
            or not isinstance(material.version, str) or not material.version
        ):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        environment = dict(material.environment)
        if (
            len(environment) != len(material.environment) or not environment
            or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
                   or not isinstance(value, str) or "\0" in value for key, value in environment.items())
            or environment.get("CLAUDE_CONFIG_DIR") != material.runtime_home
            or not Path(material.runtime_home).is_absolute()
        ):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        values = (material.credential_path, material.credential_sha256,
                  material.credential_device, material.credential_inode, material.session_id)
        if material.probe_name == "auth-negative":
            if any(value is not None for value in values):
                raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        elif (
            not all(value is not None for value in values)
            or not isinstance(material.credential_path, str)
            or not isinstance(material.credential_sha256, str)
            or len(material.credential_sha256) != 64
            or type(material.credential_device) is not int or type(material.credential_inode) is not int
            or not isinstance(material.session_id, str)
        ):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")

    def _child_request(self, request: DispatchRequest, *, intent_id: str,
                       workspace_identity: list[int]) -> dict:
        """Build the one bounded bootstrap message sent to the waiting child."""
        child_request = {
            "intent_id": intent_id, "generation": self.token.generation,
            "command": list(request.command), "workspace": request.workspace,
            "expected_head": request.expected_head,
            "workspace_identity": workspace_identity,
        }
        if request.host_material is not None:
            # The prompt is already the single reserved value in this exact
            # environment.  Do not duplicate it in the wire payload.
            child_request["host_material"] = request.host_material.execution_environment()
        elif request.native_review_material is not None:
            material = request.native_review_material
            child_request["launch_environment"] = material.execution_environment()
            guard = {"path": material.credential_path, "sha256": material.credential_sha256,
                     "device": material.credential_device, "inode": material.credential_inode}
            if material.native.host == "claude":
                guard.update(session_id=material.native.session_id, model=material.native.requested_model,
                             version=material.native.cli_version)
            child_request["claude_auth_guard" if material.native.host == "claude" else "auth_guard"] = guard
        elif request.codex_material is not None:
            child_request["launch_environment"] = request.codex_material.execution_environment()
            child_request["auth_guard"] = {
                "path": request.codex_material.auth_path,
                "sha256": request.codex_material.auth_sha256,
                "device": request.codex_material.auth_device,
                "inode": request.codex_material.auth_inode,
            }
        elif request.claude_material is not None:
            child_request["launch_environment"] = request.claude_material.execution_environment()
            child_request["claude_auth_guard"] = {
                "path": request.claude_material.credential_path,
                "sha256": request.claude_material.credential_sha256,
                "device": request.claude_material.credential_device,
                "inode": request.claude_material.credential_inode,
                "session_id": request.claude_material.session_id,
                "model": request.claude_material.model,
                "version": request.claude_material.version,
            }
        elif request.qualification_material is not None:
            child_request["launch_environment"] = request.qualification_material.execution_environment()
        elif request.claude_qualification_material is not None:
            material = request.claude_qualification_material
            child_request["launch_environment"] = material.execution_environment()
            if material.credential_path is not None:
                child_request["claude_auth_guard"] = {
                    "path": material.credential_path, "sha256": material.credential_sha256,
                    "device": material.credential_device, "inode": material.credential_inode,
                    "session_id": material.session_id, "model": material.model,
                    "version": material.version,
                }
        elif request.local_check_material is not None:
            child_request["launch_environment"] = request.local_check_material.execution_environment()
        return child_request

    def _validate_child_message_size(self, request: DispatchRequest, workspace_identity: list[int]) -> None:
        # reserve_launch creates a UUID4, which is always 36 ASCII bytes.  The
        # placeholder therefore has the exact encoded width of the durable id.
        if request.monitor_result:
            preview = self._monitor_request(request, intent_id="0" * 36, workspace_identity=workspace_identity,
                                            streams={"stdout": [9_223_372_036_854_775_807] * 2,
                                                     "stderr": [9_223_372_036_854_775_807] * 2})
        else:
            preview = self._child_request(
                request, intent_id="0" * 36, workspace_identity=workspace_identity,
            )
        if len(_canonical(preview)) > _bootstrap_frame_limit(
                native_review=request.native_review_material is not None):
            raise SupervisorRefused(
                "HOST_MATERIAL_TOO_LARGE"
                if (request.host_material is not None or request.codex_material is not None
                    or request.claude_material is not None
                    or request.qualification_material is not None)
                else "MESSAGE_TOO_LARGE"
            )

    def _resolve_review_material(self, request: DispatchRequest, preparation) -> DispatchRequest:
        """Replace caller claims with exact bytes from the retained capture."""
        claimed = (request.native_review_material.artifact if request.native_review_material is not None
                   else request.host_material)
        if claimed is None:
            return request
        try:
            resolved = artifact_review_inputs(self.store, preparation, claimed.selected_artifacts)
            requested, contents = dict(claimed.selected_artifacts), resolved["contents"]
            # The caller's populated contents are a claim only; exact capture
            # bytes must match before replacing the immutable request.
            if (claimed.selected_contents is not None
                    and dict(claimed.selected_contents) != contents):
                raise WorkspaceRefused("INPUT_SELECTION_CHANGED")
            provenance = {
                "activity_id": request.activity_id, **resolved["provenance"],
            }
            from host_capabilities import build_artifact_review_material
            material = build_artifact_review_material(
                host=claimed.host,
                model_request=dict(claimed.requested_model),
                config_sha256=claimed.config_sha256,
                policy_sha256=claimed.policy_sha256,
                environment=dict(claimed.environment),
                selected_artifacts=requested, selected_contents=contents,
                provenance=provenance,
                output_contract=(None if claimed.output_contract_json is None
                                 else json.loads(claimed.output_contract_json)),
                review_context=(None if claimed.review_context_json is None
                                else json.loads(claimed.review_context_json)),
            )
            if request.native_review_material is not None:
                # A prepared native argv already binds the exact prompt. Never
                # silently replace it after the private closure was built.
                if material != claimed:
                    raise WorkspaceRefused("INPUT_SELECTION_CHANGED")
                return request
            return replace(request, host_material=material)
        except (WorkspaceRefused, CapabilityError, OSError, UnicodeError) as error:
            raise SupervisorRefused("HOST_MATERIAL_INVALID") from error

    def _monitor_request(self, request: DispatchRequest, *, intent_id: str,
                         workspace_identity: list[int], streams: dict) -> dict:
        return {
            **self._child_request(request, intent_id=intent_id, workspace_identity=workspace_identity),
            "activity_id": request.activity_id,
            "evidence_root": str(self.evidence_root / intent_id), "streams": streams,
        }

    def _verify_physical_workspace(self, preparation, expected_identity=None):
        _verify_snapshot_complete(self.store, preparation, verify_workspace=False)
        descriptor = resolve_repository(preparation.repository_path)
        with git_admin_lock(descriptor.common_dir):
            with self.store.fenced_operation(self.token):
                if inspect_ready_layout(preparation) != "ready":
                    raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
                identity = _workspace_identity(preparation.path)
                if expected_identity is not None and identity != expected_identity:
                    raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
                return identity

    def _assert_release_binding(self, tx, request, preparation, permit):
        activity = self.store._assert_activity_binding(tx, self.token, request.activity_id)
        self.store._assert_activity_ancestry(
            tx, request.activity_id, repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        if activity["state"] != "active" or activity["runtime_tuple_hash"] != request.runtime_identity:
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        bound = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id=?", (preparation.id,),
        ).fetchone()
        from run_state.workspace import _assert_preparation_binding
        _assert_preparation_binding(bound, self.token, require_generation=True, tx=tx)
        if _from_row(bound) != preparation:
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        binding = tx.execute(
            "SELECT * FROM authority_child_bindings WHERE activity_id=?", (request.activity_id,),
        ).fetchone()
        if (binding is None or binding["workspace_preparation_id"] != preparation.id
                or binding["workspace_binding"] != request.workspace
                or binding["parent_activity_id"] != preparation.parent_activity_id
                or binding["role"] != preparation.child_role
                or binding["runtime_identity"] != request.runtime_identity
                or binding["contract_hash"] != request.contract_hash):
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        intent = tx.execute(
            "SELECT 1 FROM authority_launch_intents WHERE id=? AND activity_id=? "
            "AND generation=? AND permit_id=? AND state='released_to_execute'",
            (permit.intent_id, request.activity_id, self.token.generation, permit.id),
        ).fetchone()
        if intent is None:
            raise SupervisorRefused("FENCE_REVOKED")

    def _dispatch_material(self, request: DispatchRequest) -> dict:
        material = {
            "command_sha256": hashlib.sha256(_canonical(request.command)).hexdigest(),
            "workspace": request.workspace, "expected_head": request.expected_head,
            "runtime_identity": request.runtime_identity,
            "contract_hash": request.contract_hash,
        }
        if request.monitor_result:
            material["transport"] = "supervisor-monitor-v1"
        if request.host_material is not None:
            material["host_material"] = request.host_material.replay_binding()
        if request.native_review_material is not None:
            from .native_review_supervision import material_locator
            native = request.native_review_material
            material["native_review_material"] = {
                **native.replay_binding(), "material_locator": str(material_locator(self, native)),
                "acceptance_hash": request.contract_hash, "candidate_hash": request.managed_input_sha256,
            }
        if request.codex_material is not None:
            material["codex_material"] = {
                "runtime_sha256": request.codex_material.runtime_sha256,
                "binary": dict(request.codex_material.binary),
                "version": request.codex_material.version,
                "argv_sha256": hashlib.sha256(_canonical(request.codex_material.argv)).hexdigest(),
                "environment_sha256": hashlib.sha256(
                    _canonical(request.codex_material.environment),
                ).hexdigest(),
                "config_sha256": request.codex_material.config_sha256,
                "model": request.codex_material.model,
                "effort": request.codex_material.effort,
                "attempt": request.codex_material.attempt,
                "auth_sha256": request.codex_material.auth_sha256,
                "auth_identity": [request.codex_material.auth_device,
                                  request.codex_material.auth_inode],
            }
        if request.claude_material is not None:
            material["claude_material"] = {
                "runtime_sha256": request.claude_material.runtime_sha256,
                "binary": dict(request.claude_material.binary),
                "version": request.claude_material.version,
                "argv_sha256": hashlib.sha256(_canonical(request.claude_material.argv)).hexdigest(),
                "environment_sha256": hashlib.sha256(_canonical(request.claude_material.environment)).hexdigest(),
                "model": request.claude_material.model,
                "effort": request.claude_material.effort,
                "session_id": request.claude_material.session_id,
                "attempt": request.claude_material.attempt,
                "credential_sha256": request.claude_material.credential_sha256,
                "credential_identity": [request.claude_material.credential_device,
                                        request.claude_material.credential_inode],
            }
        if request.qualification_material is not None:
            probe = request.qualification_material
            material["qualification_material"] = {
                "schema": "ffs.codex-qualification-probe/v1",
                "probe_name": probe.probe_name,
                "contract_sha256": probe.contract_sha256,
                "envelope_sha256": probe.envelope_sha256,
                "runtime_template_sha256": probe.runtime_template_sha256,
                "argv_sha256": hashlib.sha256(_canonical(probe.argv)).hexdigest(),
                "environment_sha256": hashlib.sha256(_canonical(probe.environment)).hexdigest(),
                "runtime_home": probe.runtime_home,
            }
        if request.claude_qualification_material is not None:
            probe = request.claude_qualification_material
            material["claude_qualification_material"] = {
                "schema": "ffs.claude-qualification-probe/v1", "probe_name": probe.probe_name,
                "contract_sha256": probe.contract_sha256, "envelope_sha256": probe.envelope_sha256,
                "runtime_template_sha256": probe.runtime_template_sha256,
                "argv_sha256": hashlib.sha256(_canonical(probe.argv)).hexdigest(),
                "environment_sha256": hashlib.sha256(_canonical(probe.environment)).hexdigest(),
                "runtime_home": probe.runtime_home, "session_id": probe.session_id,
            }
        if request.local_check_material is not None:
            material["local_check_material"] = request.local_check_material.to_dict()
        return material

    def launch(self, request: DispatchRequest) -> ProcessHandle:
        """Commit intent/debit, spawn a waiting child, verify ACK, then permit."""
        return self._launch(request, managed_outer_capacity_exempt=False)

    def launch_native_review(self, request: DispatchRequest) -> ProcessHandle:
        """One restricted final review under the existing action and runtime receipt."""
        from .native_review_supervision import publish_material
        if request.native_review_material is None:
            raise SupervisorRefused("NATIVE_REVIEW_MATERIAL_INVALID")
        with self._dispatch_lock:
            preparation = self._validate(request)
            request = self._resolve_review_material(request, preparation)
            self._validate_child_message_size(request, self._verify_physical_workspace(preparation))
            publish_material(self, request.native_review_material)
            request = self.reserve_request_action(request, action="final_review")
            return self._launch(request, managed_outer_capacity_exempt=False, native_review=True)

    def launch_sealed_check(self, request: DispatchRequest, *, acceptance_hash: str,
                            check_id: str) -> ProcessHandle:
        """Launch one sealed deterministic command through the normal child fence.

        Callers supply a registered child request only.  Command, environment,
        executable closure, and receipt are built here from the active seal.
        """
        if request.native_review_material is not None:
            raise SupervisorRefused("NATIVE_REVIEW_ENTRYPOINT_REQUIRED")
        with self._dispatch_lock:
            with self.store.read_transaction() as tx:
                child = tx.execute(
                    "SELECT candidate_hash,workspace_preparation_id FROM authority_child_bindings WHERE activity_id=?",
                    (request.activity_id,),
                ).fetchone()
            if child is None or request.contract_hash != acceptance_hash:
                raise SupervisorRefused("LOCAL_CHECK_BINDING_INVALID")
            sealed = self.store.get_sealed_acceptance(
                repository_id=self.token.repository_id, run_id=self.token.run_id,
            )
            try:
                material = sealed_check_material(
                    sealed=sealed, acceptance_hash=acceptance_hash, check_id=check_id,
                    candidate_hash=child["candidate_hash"], workspace=request.workspace,
                    workspace_preparation_id=child["workspace_preparation_id"],
                    expected_head=request.expected_head, runtime_identity=request.runtime_identity,
                    generation=self.token.generation,
                )
                from .local_check_runtime import build_confined_local_argv, verify_local_candidate
                verify_local_candidate(self.store, material)
                material, command, _policy = build_confined_local_argv(
                    self.store, self.token, request.activity_id, material)
            except LocalCheckRefused as error:
                raise SupervisorRefused(error.code) from error
            try:
                receipt = self.store.commit_local_check_receipt(self.token, request.activity_id, material)
            except ControlStoreRefused as error:
                raise SupervisorRefused(error.code) from error
            bound = replace(
                request, command=command, managed_input_sha256=child["candidate_hash"], monitor_result=False,
                local_check_material=material, local_check_receipt_sha256=receipt.receipt_sha256,
                runtime_receipt_sha256=None,
            )
            self._local_check_commands[material.material_sha256] = command
            bound = self.reserve_request_action(bound, action="check")
            return self._launch(bound, managed_outer_capacity_exempt=False)

    def launch_managed_outer(self, request: DispatchRequest) -> ProcessHandle:
        """Launch the one managed host orchestrator outside the worker pool.

        This is intentionally a supervisor-owned path rather than a request
        field. ControlStore revalidates the managed-root binding and records
        the immutable exemption with the dispatch replay material.
        """
        if request.native_review_material is not None:
            raise SupervisorRefused("NATIVE_REVIEW_ENTRYPOINT_REQUIRED")
        with self.store.read_transaction() as tx:
            child = tx.execute("SELECT role FROM authority_child_bindings WHERE activity_id=?", (request.activity_id,)).fetchone()
        action = "final_review" if child is not None and child["role"] == "reviewer" else "execute"
        request = self.reserve_request_action(replace(request, monitor_result=True), action=action)
        return self._launch(request, managed_outer_capacity_exempt=True)

    def _launch(self, request: DispatchRequest, *, managed_outer_capacity_exempt: bool,
                native_review: bool = False) -> ProcessHandle:
        if (request.native_review_material is not None) != native_review:
            raise SupervisorRefused("NATIVE_REVIEW_ENTRYPOINT_REQUIRED")
        with self._dispatch_lock:
            preparation = self._validate(request)
            workspace_identity = self._verify_physical_workspace(preparation)
            request = self._resolve_review_material(request, preparation)
            self._validate_child_message_size(request, workspace_identity)
            admission_guard = self._delegate_launch_guard(request, preparation)
            if (managed_outer_capacity_exempt
                    and (request.codex_material is not None or request.claude_material is not None)):
                from .managed_resource_group import ManagedParentResourceCoordinator
                if (not isinstance(self.shared_resource_coordinator, ManagedParentResourceCoordinator)
                        or self.shared_resource_coordinator.parent_request_key != request.activity_id + ':' + request.request_key):
                    raise SupervisorRefused("RESOURCE_PARENT_GROUP_REQUIRED")
            # The request event and the launch debit share reserve_launch's
            # transaction. A replay can therefore resolve its exact durable
            # intent, including a completed one, without a second spawn.
            material = self._dispatch_material(request)
            shared = self._reserve_shared_resources((request,))
            intent = self.store.reserve_launch(
                request.activity_id, self.token, token_reservation=request.token_reservation,
                request_key=request.request_key, request_payload=material,
                admission_guard=admission_guard,
                runtime_receipt_sha256=request.runtime_receipt_sha256,
                local_check_receipt_sha256=request.local_check_receipt_sha256,
                managed_input_sha256=request.managed_input_sha256,
                managed_outer_capacity_exempt=managed_outer_capacity_exempt,
                policy_action_id=request.policy_action_id,
            )
            if intent.reused:
                if intent.state in {"completed_succeeded", "completed_failed"}:
                    raise SupervisorRefused("REQUEST_ALREADY_COMPLETED")
                raise SupervisorRefused("INTENT_RECONCILIATION_REQUIRED")
            if shared:
                self._bind_shared_resource(intent.id, shared[0])
            self._fault("after_intent_commit")
            return self._spawn(request, intent.id, preparation, workspace_identity, admission_guard=admission_guard)

    def launch_qualification(
        self, request: DispatchRequest, *, qualification_contract: dict,
    ) -> ProcessHandle:
        """Admit one fixed pre-receipt probe through the normal child fence."""
        if request.native_review_material is not None:
            raise SupervisorRefused("NATIVE_REVIEW_ENTRYPOINT_REQUIRED")
        if (
            (request.qualification_material is None and request.claude_qualification_material is None)
            or request.managed_input_sha256 is None
        ):
            raise SupervisorRefused("QUALIFICATION_MATERIAL_INVALID")
        with self._dispatch_lock:
            preparation = self._validate(request)
            workspace_identity = self._verify_physical_workspace(preparation)
            self._validate_child_message_size(request, workspace_identity)
            request = self.reserve_request_action(
                request, action="qualification", qualification_contract=qualification_contract,
            )
            shared = self._reserve_shared_resources((request,))
            intent = self.store.reserve_qualification_launch(
                request.activity_id, self.token, request_key=request.request_key,
                qualification_contract=qualification_contract,
                token_reservation=request.token_reservation,
                managed_input_sha256=request.managed_input_sha256,
                policy_action_id=request.policy_action_id,
            )
            if intent.reused:
                if intent.state in {"completed_succeeded", "completed_failed"}:
                    raise SupervisorRefused("REQUEST_ALREADY_COMPLETED")
                raise SupervisorRefused("INTENT_RECONCILIATION_REQUIRED")
            if shared:
                self._bind_shared_resource(intent.id, shared[0])
            self._fault("after_intent_commit")
            return self._spawn(
                request, intent.id, preparation, workspace_identity,
                admission_guard=None,
            )

    def launch_cohort(self, requests: tuple[DispatchRequest, ...], *, request_key: str,
                      admission_guard=None) -> tuple[ProcessHandle, ...]:
        """Reserve a complete wave, ACK every waiting child, then release all.

        A failure before the atomic release intentionally leaves the retained
        cohort and all debits for explicit reconciliation.  No elapsed-time
        inference refunds or relaunches an uncertain member.
        """
        if (not isinstance(requests, tuple) or not requests
                or not isinstance(request_key, str) or not request_key
                or len({item.activity_id for item in requests}) != len(requests)
                or len({item.request_key for item in requests}) != len(requests)):
            raise SupervisorRefused("INVALID_DISPATCH")
        if any(request.native_review_material is not None for request in requests):
            raise SupervisorRefused("NATIVE_REVIEW_ENTRYPOINT_REQUIRED")
        with self._dispatch_lock:
            prepared = []
            for request in requests:
                preparation = self._validate(request)
                workspace_identity = self._verify_physical_workspace(preparation)
                request = self._resolve_review_material(request, preparation)
                self._validate_child_message_size(request, workspace_identity)
                request = self.reserve_request_action(request, action="execute")
                prepared.append((request, preparation, workspace_identity))
            members = tuple({
                "activity_id": request.activity_id,
                "request_key": request.request_key,
                "request_payload": self._dispatch_material(request),
                "token_reservation": request.token_reservation,
                "runtime_receipt_sha256": request.runtime_receipt_sha256,
                "managed_input_sha256": request.managed_input_sha256,
            } for request, _preparation, _identity in prepared)
            shared = self._reserve_shared_resources(tuple(item[0] for item in prepared), group_key=request_key)
            cohort = self.store.reserve_launch_cohort(
                self.token, request_key=request_key, members=members,
                admission_guard=admission_guard,
                policy_action_ids={request.request_key: request.policy_action_id
                                   for request, _preparation, _identity in prepared
                                   if request.policy_action_id is not None} or None,
            )
            if cohort.reused:
                raise SupervisorRefused("COHORT_RECONCILIATION_REQUIRED")
            intents = {intent.activity_id: intent for intent in cohort.members}
            if shared:
                for (request, _preparation, _identity), reservation in zip(prepared, shared):
                    self._bind_shared_resource(intents[request.activity_id].id, reservation)
            waiting: list[_WaitingCohortChild] = []
            try:
                for request, preparation, workspace_identity in prepared:
                    intent = intents.get(request.activity_id)
                    if intent is None:
                        raise SupervisorRefused("COHORT_BINDING_UNKNOWN")
                    waiting.append(self._spawn_cohort_waiting(
                        cohort.id, request, intent.id, preparation, workspace_identity,
                        admission_guard=admission_guard,
                    ))
                permits = {
                    permit.intent_id: permit for permit in self.store.release_launch_cohort(
                        cohort.id, self.token, admission_guard=admission_guard,
                        **self._policy_clock(),
                    )
                }
                handles = []
                for child in waiting:
                    permit = permits.get(child.intent_id)
                    if permit is None:
                        raise SupervisorRefused("COHORT_BINDING_UNKNOWN")
                    worker_binding = {}
                    if self.worker_channel is not None:
                        scope = self.worker_channel.register_worker(
                            child.intent_id, contract_hash=child.request.contract_hash,
                        )
                        worker_binding = {
                            "worker_endpoint": str(self.worker_channel.endpoint),
                            "worker_scope": scope,
                        }
                        if (child.request.codex_material is not None
                                or child.request.claude_material is not None):
                            worker_binding["worker_file_channel"] = (
                                self.worker_channel.register_file_transport(child.intent_id)
                            )
                    self._verify_physical_workspace(child.preparation, child.workspace_identity)
                    with self.store.fenced_operation(self.token):
                        with self.store.transaction() as tx:
                            if admission_guard is not None:
                                admission_guard(tx)
                            if probe_identity(child.identity) != LIVE:
                                raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
                            self._assert_release_binding(
                                tx, child.request, child.preparation, permit,
                            )
                        _send(child.channel, {
                            "permit_id": permit.id, "intent_id": child.intent_id,
                            "generation": self.token.generation, "authorized": True,
                            **worker_binding,
                        })
                    child.channel.close()
                    handle = ProcessHandle(
                        child.process, child.intent_id, child.request.activity_id,
                        child.identity, child.request.expected_head, time.time(),
                        child.stdout_path, child.stderr_path, child.stream_identities,
                        monitor_identity=child.monitor_identity,
                        monitored=child.request.monitor_result,
                        replay_material=self._dispatch_material(child.request),
                        codex_material=child.request.codex_material,
                        claude_material=child.request.claude_material,
                        qualification_material=child.request.qualification_material,
                        claude_qualification_material=child.request.claude_qualification_material,
                    )
                    self._handles[child.intent_id] = handle
                    handles.append(handle)
                return tuple(handles)
            except BaseException:
                for child in waiting:
                    try:
                        child.channel.close()
                    except OSError:
                        pass
                    try:
                        child.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    self.store.reconcile_launch_cohort(cohort.id, self.token)
                except Exception:
                    pass
                raise

    def _spawn_cohort_waiting(self, cohort_id: str, request: DispatchRequest,
                              intent_id: str, preparation, workspace_identity,
                              *, admission_guard=None) -> _WaitingCohortChild:
        """Spawn and ACK one cohort member without authorizing execution."""
        self._assert_shared_spawn_safe(intent_id)
        parent, child = socket.socketpair()
        parent.settimeout(30)
        root = self.evidence_root / intent_id
        root_fd = _open_directory_chain_raw(Path(root.anchor), root.parts[1:], create=True)
        stdout_path, stderr_path = root / "stdout.log", root / "stderr.log"
        env = sanitized_git_environment()
        for key in tuple(env):
            if key.startswith(("FFS_", "GSD_")) or key in {
                "RUN_STATE_DB", "GATES_STORE", "PYTHONPATH", "PYTHONSTARTUP",
            }:
                del env[key]
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        proc = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            stdout_fd = os.open("stdout.log", flags, 0o600, dir_fd=root_fd)
            try:
                stderr_fd = os.open("stderr.log", flags, 0o600, dir_fd=root_fd)
            except BaseException:
                os.close(stdout_fd)
                raise
            with os.fdopen(stdout_fd, "wb") as stdout, os.fdopen(stderr_fd, "wb") as stderr:
                identities = {}
                for key, stream in (("stdout", stdout), ("stderr", stderr)):
                    info = os.fstat(stream.fileno())
                    identities[key] = (info.st_dev, info.st_ino)
                self._verify_physical_workspace(preparation, workspace_identity)
                with self.store.fenced_operation(self.token):
                    if admission_guard is not None:
                        with self.store.transaction() as tx:
                            admission_guard(tx)
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "run_state.supervisor",
                         "_monitor" if request.monitor_result else "_child", str(child.fileno())],
                        cwd=request.workspace, env=env, pass_fds=(child.fileno(),),
                        stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                        start_new_session=True,
                    )
            child.close()
            bootstrap = self._monitor_request(
                request, intent_id=intent_id, workspace_identity=workspace_identity,
                streams={key: list(value) for key, value in identities.items()},
            ) if request.monitor_result else self._child_request(
                request, intent_id=intent_id, workspace_identity=workspace_identity,
            )
            _send(parent, bootstrap)
            reply = _receive(parent)
            identity = ProcessIdentity(**reply["native" if request.monitor_result else "identity"])
            monitor_identity = ProcessIdentity(**reply["monitor"]) if request.monitor_result else None
            spawned_identity = monitor_identity if request.monitor_result else identity
            if (reply.get("intent_id") != intent_id or spawned_identity.pid != proc.pid
                    or reply.get("initial_head") != request.expected_head
                    or reply.get("workspace_identity") != workspace_identity):
                raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
            if request.monitor_result:
                self.store.acknowledge_monitored_cohort_child(
                    cohort_id, intent_id, self.token, monitor_identity, identity,
                    {"transport": "supervisor-monitor-v1", "monitor": asdict(monitor_identity),
                     "native": asdict(identity), "initial_head": reply["initial_head"],
                     "workspace": request.workspace, "workspace_identity": workspace_identity,
                     "streams": {key: list(value) for key, value in identities.items()}},
                    admission_guard=admission_guard,
                )
            else:
                self.store.acknowledge_cohort_child(
                    cohort_id, intent_id, self.token, identity,
                    admission_guard=admission_guard,
                )
                self.store.record_event_once(
                    self.token, request.activity_id, "child-ack:" + intent_id,
                    {"identity": asdict(identity), "initial_head": reply["initial_head"],
                     "workspace": request.workspace, "cohort_id": cohort_id},
                )
            self._bind_shared_resource(intent_id, consumer=identity)
            return _WaitingCohortChild(
                request, preparation, workspace_identity, proc, parent, intent_id,
                identity, stdout_path, stderr_path, identities, monitor_identity,
            )
        except BaseException:
            parent.close()
            child.close()
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
        finally:
            child.close()
            os.close(root_fd)

    def _spawn(self, request: DispatchRequest, intent_id: str, preparation, workspace_identity, *, admission_guard=None) -> ProcessHandle:
        # A receipt is a verified launch contract, never an authorization to
        # execute bytes which changed after reservation.
        if request.local_check_material is not None:
            self._validate_local_check_material(request)
        self._assert_shared_spawn_safe(intent_id)
        if request.monitor_result:
            return self._spawn_monitored(request, intent_id, preparation, workspace_identity)
        parent, child = socket.socketpair()
        parent.settimeout(15)
        root = self.evidence_root / intent_id
        root_fd = _open_directory_chain_raw(
            Path(root.anchor), root.parts[1:], create=True,
        )
        stdout_path, stderr_path = root / "stdout.log", root / "stderr.log"
        env = sanitized_git_environment()
        for key in tuple(env):
            if key.startswith(("FFS_", "GSD_")) or key in {"RUN_STATE_DB", "GATES_STORE", "PYTHONPATH", "PYTHONSTARTUP"}:
                del env[key]
        # The child receives no owner token or database path.
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        proc = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            stdout_fd = os.open("stdout.log", flags, 0o600, dir_fd=root_fd)
            try:
                stderr_fd = os.open("stderr.log", flags, 0o600, dir_fd=root_fd)
            except BaseException:
                os.close(stdout_fd)
                raise
            with os.fdopen(stdout_fd, "wb") as stdout, os.fdopen(stderr_fd, "wb") as stderr:
                identities = {}
                for key, stream in (("stdout", stdout), ("stderr", stderr)):
                    info = os.fstat(stream.fileno())
                    identities[key] = (info.st_dev, info.st_ino)
                self._verify_physical_workspace(preparation, workspace_identity)
                with self.store.fenced_operation(self.token):
                    if admission_guard is not None:
                        with self.store.transaction() as tx:
                            admission_guard(tx)
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "run_state.supervisor", "_child", str(child.fileno())],
                        cwd=request.workspace, env=env, pass_fds=(child.fileno(),),
                        stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                        start_new_session=True,
                    )
            child.close()
            self._fault("after_spawn_before_ack")
            _send(parent, self._child_request(
                request, intent_id=intent_id, workspace_identity=workspace_identity,
            ))
            reply = _receive(parent)
            identity = ProcessIdentity(**reply["identity"])
            if (reply.get("intent_id") != intent_id or identity.pid != proc.pid
                    or reply.get("initial_head") != request.expected_head
                    or reply.get("workspace_identity") != workspace_identity):
                raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
            acknowledgement = self.store.acknowledge_child(
                intent_id, self.token, identity, admission_guard=admission_guard,
            )
            self._bind_shared_resource(intent_id, consumer=identity)
            self.store.record_event_once(
                self.token, request.activity_id, "child-ack:" + intent_id,
                {"identity": asdict(identity), "initial_head": reply["initial_head"],
                 "workspace": request.workspace},
            )
            self._fault("after_ack_before_authorization")
            self._validate(request)
            self._verify_physical_workspace(preparation, workspace_identity)
            permit = self.store.authorize_child(
                acknowledgement, self.token, admission_guard=admission_guard,
                **self._policy_clock(),
            )
            self._fault("after_authorization_before_release")
            worker_binding = {}
            if self.worker_channel is not None:
                scope = self.worker_channel.register_worker(intent_id, contract_hash=request.contract_hash)
                worker_binding = {"worker_endpoint": str(self.worker_channel.endpoint), "worker_scope": scope}
                if request.codex_material is not None or request.claude_material is not None:
                    worker_binding["worker_file_channel"] = self.worker_channel.register_file_transport(intent_id)
            self._verify_physical_workspace(preparation, workspace_identity)
            with self.store.fenced_operation(self.token):
                with self.store.transaction() as tx:
                    if admission_guard is not None:
                        admission_guard(tx)
                        if probe_identity(identity) != LIVE:
                            raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
                    self._assert_release_binding(tx, request, preparation, permit)
                _send(parent, {"permit_id": permit.id, "intent_id": intent_id,
                               "generation": self.token.generation, "authorized": True, **worker_binding})
            handle = ProcessHandle(proc, intent_id, request.activity_id, identity,
                                   reply["initial_head"], time.time(), stdout_path, stderr_path, identities,
                                   codex_material=request.codex_material,
                                   claude_material=request.claude_material,
                                   qualification_material=request.qualification_material,
                                   claude_qualification_material=request.claude_qualification_material,
                                   local_check_material=request.local_check_material)
            self._handles[intent_id] = handle
            return handle
        except BaseException:
            # Closing the channel makes an unpermitted child exit. Leave the
            # durable debit/intent intact even if no ACK was committed.
            parent.close()
            child.close()
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # execution may have begun; never infer death
            raise
        finally:
            parent.close()
            child.close()
            os.close(root_fd)

    def _spawn_monitored(self, request: DispatchRequest, intent_id: str, preparation,
                         workspace_identity) -> ProcessHandle:
        """Start one monitor and its one waiting native child after reservation.

        The monitor only owns an already admitted attempt's wait result.  It
        has neither a ControlStore handle nor permit-generation authority.
        """
        parent, monitor_channel = socket.socketpair()
        parent.settimeout(15)
        root = self.evidence_root / intent_id
        root_fd = _open_directory_chain_raw(Path(root.anchor), root.parts[1:], create=True)
        stdout_path, stderr_path = root / "stdout.log", root / "stderr.log"
        env = sanitized_git_environment()
        for key in tuple(env):
            if key.startswith(("FFS_", "GSD_")) or key in {"RUN_STATE_DB", "GATES_STORE", "PYTHONPATH", "PYTHONSTARTUP"}:
                del env[key]
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        proc = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            stdout_fd = os.open("stdout.log", flags, 0o600, dir_fd=root_fd)
            try:
                stderr_fd = os.open("stderr.log", flags, 0o600, dir_fd=root_fd)
            except BaseException:
                os.close(stdout_fd)
                raise
            with os.fdopen(stdout_fd, "wb") as stdout, os.fdopen(stderr_fd, "wb") as stderr:
                identities = {}
                for key, stream in (("stdout", stdout), ("stderr", stderr)):
                    info = os.fstat(stream.fileno())
                    identities[key] = (info.st_dev, info.st_ino)
                self._verify_physical_workspace(preparation, workspace_identity)
                proc = subprocess.Popen(
                    [sys.executable, "-m", "run_state.supervisor",
                     "_monitor_native" if request.native_review_material is not None else "_monitor",
                     str(monitor_channel.fileno())],
                    cwd=request.workspace, env=env, pass_fds=(monitor_channel.fileno(),),
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True,
                )
            monitor_channel.close()
            self._fault("after_spawn_before_ack")
            _send(parent, self._monitor_request(
                request, intent_id=intent_id, workspace_identity=workspace_identity,
                streams={key: list(value) for key, value in identities.items()},
            ), max_bytes=_bootstrap_frame_limit(
                native_review=request.native_review_material is not None,
            ))
            reply = _receive(parent)
            monitor_identity = ProcessIdentity(**reply["monitor"])
            identity = ProcessIdentity(**reply["native"])
            if (monitor_identity.pid != proc.pid or reply.get("intent_id") != intent_id
                    or reply.get("initial_head") != request.expected_head
                    or reply.get("workspace_identity") != workspace_identity):
                raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
            binding = {
                "transport": "supervisor-monitor-v1", "monitor": asdict(monitor_identity),
                "native": asdict(identity), "initial_head": reply["initial_head"],
                "workspace": request.workspace, "workspace_identity": workspace_identity,
                "streams": {key: list(value) for key, value in identities.items()},
            }
            acknowledgement = self.store.acknowledge_monitored_child(
                intent_id, self.token, monitor_identity, identity, binding,
            )
            self._bind_shared_resource(intent_id, consumer=identity)
            self._fault("after_ack_before_authorization")
            self._validate(request)
            self._verify_physical_workspace(preparation, workspace_identity)
            permit = self.store.authorize_child(acknowledgement, self.token, **self._policy_clock())
            self._fault("after_authorization_before_release")
            worker_binding = {}
            if self.worker_channel is not None:
                worker_binding = {
                    "worker_endpoint": str(self.worker_channel.endpoint),
                    "worker_scope": self.worker_channel.register_worker(
                        intent_id, contract_hash=request.contract_hash,
                    ),
                }
                if request.codex_material is not None or request.claude_material is not None:
                    worker_binding["worker_file_channel"] = self.worker_channel.register_file_transport(intent_id)
            self._verify_physical_workspace(preparation, workspace_identity)
            if request.native_review_material is not None:
                self._validate(request)
            with self.store.fenced_operation(self.token):
                with self.store.transaction() as tx:
                    self._assert_release_binding(tx, request, preparation, permit)
                _send(parent, {"permit_id": permit.id, "intent_id": intent_id,
                               "generation": self.token.generation, "authorized": True, **worker_binding})
            handle = ProcessHandle(proc, intent_id, request.activity_id, identity,
                                   reply["initial_head"], time.time(), stdout_path, stderr_path,
                                   identities, monitor_identity=monitor_identity, monitored=True,
                                   replay_material=self._dispatch_material(request))
            self._handles[intent_id] = handle
            return handle
        except BaseException:
            parent.close()
            monitor_channel.close()
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
        finally:
            parent.close()
            monitor_channel.close()
            os.close(root_fd)

    def resume_monitored(self, intent_id: str) -> ProcessHandle:
        """Reconstruct observation of one already-released monitor transport.

        A future qualified adapter may call this before considering a new
        launch.  It observes the original permit only; it never ACKs, permits,
        debits, or starts another process.
        """
        if not isinstance(intent_id, str) or not intent_id:
            raise SupervisorRefused("MONITOR_BINDING_INVALID")
        with self.store.read_transaction() as tx:
            row = tx.execute("SELECT * FROM authority_launch_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None or not row["permit_id"] or row["child_pid"] is None:
                raise SupervisorRefused("MONITOR_BINDING_INVALID")
            event = tx.execute(
                "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                "WHERE k.activity_id=? AND k.idempotency_key=?", (row["activity_id"], "child-ack:" + intent_id),
            ).fetchone()
            request_event = tx.execute(
                "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                "WHERE k.activity_id=? AND k.idempotency_key LIKE 'dispatch-request:%'", (row["activity_id"],),
            ).fetchall()
        try:
            binding = json.loads(event["payload"])["data"]
            dispatches = [json.loads(item["payload"])["data"] for item in request_event]
            dispatch = next(item for item in dispatches if item["intent_id"] == intent_id)
            request = dispatch["request"]
            monitor_identity = ProcessIdentity(**binding["monitor"])
            identity = ProcessIdentity(**binding["native"])
            workspace = Path(binding["workspace"])
            stream_identity = binding.get("streams")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, StopIteration) as error:
            raise SupervisorRefused("MONITOR_BINDING_INVALID") from error
        if (
            binding.get("transport") != "supervisor-monitor-v1"
            or binding.get("intent_id") != intent_id
            or binding.get("issuing_generation") != row["generation"]
            or binding.get("acknowledgement_id") != row["acknowledgement_id"]
            or identity != ProcessIdentity(row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"])
            or request.get("transport") != "supervisor-monitor-v1"
            or not workspace.is_absolute()
        ):
            raise SupervisorRefused("MONITOR_BINDING_INVALID")
        root = self.evidence_root / intent_id
        paths = (root / "stdout.log", root / "stderr.log")
        identities = {}
        for key, path in zip(("stdout", "stderr"), paths):
            try:
                info = path.stat()
            except OSError as error:
                raise SupervisorRefused("MONITOR_RESULT_UNAVAILABLE") from error
            identities[key] = (info.st_dev, info.st_ino)
        if stream_identity is not None and stream_identity != {key: list(value) for key, value in identities.items()}:
            raise SupervisorRefused("EVIDENCE_CHANGED")
        handle = ProcessHandle(None, intent_id, row["activity_id"], identity,
                               binding["initial_head"], time.time(), paths[0], paths[1], identities,
                               monitor_identity=monitor_identity, monitored=True, replay_material=request)
        self._initialize_shared_resources()
        from .shared_resources import SharedResourceCoordinator
        if isinstance(self.shared_resource_coordinator, SharedResourceCoordinator):
            reservation = self.shared_resource_coordinator.restore_bound_intent(intent_id)
            if reservation is not None:
                self._shared_reservations[intent_id] = reservation
        self._handles[intent_id] = handle
        return handle

    def contain_revoked(self) -> tuple[dict, ...]:
        """Contain terminally revoked descendants, retaining uncertainty."""
        from .containment import contain_session

        with self.store.transaction() as tx:
            from .ownership import assert_owner
            assert_owner(tx, self.token)
            rows = tx.execute(
                "SELECT i.* FROM authority_launch_intents i JOIN authority_activities a ON a.id=i.activity_id "
                "WHERE a.repository_id=? AND a.run_id=? AND i.state='reconcile_required' AND i.permit_id IS NULL",
                (self.token.repository_id, self.token.run_id),
            ).fetchall()
        reports = []
        for row in rows:
            identity = (None if row["child_pid"] is None else ProcessIdentity(
                row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"]))
            report = ({"status": "uncertain", "reason": "unacknowledged process identity"}
                      if identity is None else contain_session(identity))
            report = {"intent_id": row["id"], **report}
            handle = self._handles.get(row["id"])
            if handle is not None and handle.process is not None:
                try:
                    handle.process.wait(timeout=2)
                    report["direct_child_reaped"] = True
                except subprocess.TimeoutExpired:
                    report["direct_child_reaped"] = False
            key = "containment:" + row["id"] + ":" + hashlib.sha256(_canonical(report)).hexdigest()
            self.store.record_event_once(self.token, row["activity_id"], key, report)
            self.store.record_contained_process(self.token, intent_id=row["id"], report=report)
            reports.append(report)
        return tuple(reports)

    def expire_launch(self, handle: ProcessHandle, *, reason: str) -> None:
        if self._handles.get(handle.intent_id) is not handle:
            raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
        activity = self.store.get_activity(handle.activity_id)
        if activity.state not in {"succeeded", "failed", "aborted"}:
            self.store.transition_activity(self.token, activity.id, expected=activity.state,
                                           new="failed", reason=reason)
        self.contain_revoked()

    def _policy_timeout(self, timeout: float | None) -> tuple[float | None, bool]:
        budget = self.store.get_run_policy_budget(
            repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        if budget is None:
            return timeout, False
        try:
            self.store._preflight_policy_clock(self.token)
        except OwnershipRefused as error:
            if error.code == "ACTIVE_TIME_EXHAUSTED":
                self.expire_policy_run(reason=error.code)
            raise
        budget = self.store.get_run_policy_budget(
            repository_id=self.token.repository_id, run_id=self.token.run_id,
        )
        remaining = max(0, budget.active_limit_ns - budget.active_ns) / 1_000_000_000
        return (remaining, True) if timeout is None or remaining <= timeout else (timeout, False)

    def _wait_admitted(self, handle: ProcessHandle, timeout: float | None) -> int:
        completed = handle.process.poll() if handle.process is not None else None
        if completed is not None:
            return completed
        if handle.process is None:
            if handle.monitor_identity is None:
                raise SupervisorRefused("MONITOR_BINDING_INVALID")
            status = probe_identity(handle.monitor_identity)
            if status == DEAD:
                return 0  # The immutable monitor result supplies the exit code.
            if status != LIVE:
                raise SupervisorRefused("MONITOR_RESULT_PENDING")
        if timeout is not None:
            with self.store.read_transaction() as tx:
                row = tx.execute(
                    "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                    "WHERE k.activity_id=? AND k.idempotency_key=?",
                    (handle.activity_id, "launch-release-clock:" + handle.intent_id),
                ).fetchone()
            if row is None:
                raise SupervisorRefused("MONITOR_DEADLINE_UNPROVEN")
            released = json.loads(row["payload"])["data"]
            now_ns = time.monotonic_ns()
            if (released.get("boot_id") != ProcessIdentity.current().boot_id
                    or type(released.get("monotonic_ns")) is not int
                    or now_ns < released["monotonic_ns"]):
                raise SupervisorRefused("CLOCK_RECONCILIATION_REQUIRED")
            timeout = max(0.0, timeout - (now_ns - released["monotonic_ns"]) / 1_000_000_000)
        timeout, policy_deadline = self._policy_timeout(timeout)
        if handle.process is None and timeout is None:
            raise SupervisorRefused("MONITOR_RESULT_PENDING")
        try:
            if handle.process is not None:
                return handle.process.wait(timeout=timeout)
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                status = probe_identity(handle.monitor_identity)
                if status == DEAD:
                    return 0
                if status != LIVE:
                    raise SupervisorRefused("MONITOR_RESULT_PENDING")
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("retained monitor", timeout)
                time.sleep(.05)
        except subprocess.TimeoutExpired:
            if policy_deadline:
                self.store.record_policy_wait(self.token, kind="capacity", elapsed_ns=0, **self._policy_clock())
                self.expire_policy_run(reason="ACTIVE_TIME_EXHAUSTED")
                raise SupervisorRefused("ACTIVE_TIME_EXHAUSTED") from None
            self.expire_launch(handle, reason="admitted action deadline exceeded")
            raise SupervisorRefused("CHILD_DEADLINE_EXCEEDED") from None

    def expire_policy_run(self, *, reason: str) -> None:
        """Revoke the run's whole subtree before process containment."""
        with self.store.read_transaction() as tx:
            row = tx.execute("SELECT activity_id FROM context_runs WHERE repository_id=? AND run_id=?",
                             (self.token.repository_id, self.token.run_id)).fetchone()
        if row is None:
            raise SupervisorRefused("RUN_NOT_FOUND")
        activity = self.store.get_activity(row["activity_id"])
        if activity.state not in {"succeeded", "failed", "aborted"}:
            self.store.transition_activity(self.token, activity.id, expected=activity.state,
                                           new="failed", reason=reason)
        self.contain_revoked()

    def finish(self, handle: ProcessHandle, *, timeout: float | None = None,
               token_usage: int | None = None) -> dict:
        """Harvest actual process output before recording a terminal outcome."""
        if self._handles.get(handle.intent_id) is not handle:
            raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
        if handle.recorded:
            return handle.result
        native_review = "native_review_material" in (handle.replay_material or {})
        if native_review and token_usage is not None:
            raise SupervisorRefused("NATIVE_REVIEW_USAGE_CALLER_FORBIDDEN")
        if handle.local_check_material is not None and token_usage is not None:
            raise SupervisorRefused("LOCAL_CHECK_USAGE_CALLER_FORBIDDEN")
        if handle.local_check_material is not None and (timeout is None or timeout > 300):
            timeout = 300
        if handle.monitored:
            self._wait_admitted(handle, timeout)
            result_path = handle.stdout_path.parent / "result.json"
            try:
                raw = _read_evidence(result_path)
                result = json.loads(raw)
            except (OSError, ValueError, UnicodeError) as error:
                raise SupervisorRefused("MONITOR_RESULT_UNAVAILABLE") from error
            if (not isinstance(result, dict) or result.get("intent_id") != handle.intent_id
                    or result.get("activity_id") != handle.activity_id
                    or result.get("identity") != asdict(handle.identity)
                    or result.get("initial_head") != handle.initial_head
                    or type(result.get("returncode")) is not int):
                raise SupervisorRefused("MONITOR_RESULT_INVALID")
            streams = result.get("streams")
            if not isinstance(streams, dict):
                raise SupervisorRefused("MONITOR_RESULT_INVALID")
            for key, path in (("stdout", handle.stdout_path), ("stderr", handle.stderr_path)):
                stream = streams.get(key)
                raw_stream = _read_evidence(path, handle.stream_identities[key])
                if (not isinstance(stream, dict) or stream.get("locator") != str(path)
                        or stream.get("bytes") != len(raw_stream)
                        or stream.get("sha256") != hashlib.sha256(raw_stream).hexdigest()):
                    raise SupervisorRefused("EVIDENCE_CHANGED")
            returncode = result["returncode"]
            evidence = {"locator": str(result_path), "sha256": hashlib.sha256(raw).hexdigest()}
            replay = handle.replay_material or {}
            host = "codex" if "codex_material" in replay else "claude" if "claude_material" in replay else None
            if native_review:
                from .native_review_supervision import completion
                raw_stdout = _read_evidence(handle.stdout_path, handle.stream_identities["stdout"])
                receipt, token_usage = completion(self, handle, result, raw_stdout)
                result = {**result, "host_receipt": receipt, "monitor_evidence": evidence}
            elif host is not None:
                # Reparse verified stream bytes on every recovery.  The raw
                # monitor receipt remains immutable; the owner adds a sidecar.
                material = replay[host + "_material"]
                token_usage = None
                receipt = {"schema": f"ffs.{host}-invocation-receipt/v1",
                           "runtime_sha256": material["runtime_sha256"],
                           "attempt": material["attempt"], "exit_code": returncode,
                           "status": "uncertain", "telemetry_sha256": streams["stdout"]["sha256"]}
                try:
                    if result.get("auth_revoked") is not True:
                        raise TelemetryRefused("AUTH_REVOCATION_UNPROVEN")
                    raw_stdout = _read_evidence(handle.stdout_path, handle.stream_identities["stdout"])
                    telemetry = (parse_codex_telemetry(raw_stdout) if host == "codex" else
                                 parse_claude_telemetry(raw_stdout, requested_model=material["model"],
                                     expected_session_id=material["session_id"], expected_version=material["version"]))
                    usage = dict(telemetry.token_usage)
                    token_usage = (usage["input_tokens"] + usage["cache_write_input_tokens"]
                                   + usage["output_tokens"] + usage["reasoning_output_tokens"]
                                   if host == "codex" else usage["input_tokens"]
                                   + usage["cache_creation_input_tokens"] + usage["output_tokens"])
                    receipt.update({key: value for key, value in material.items()
                                    if key not in {"auth_identity", "credential_identity"}})
                    receipt.update(cwd=replay["workspace"], token_usage=usage,
                                   telemetry_sha256=telemetry.sha256, status="complete")
                    if host == "codex":
                        receipt.update(auth_revoked=True, thread_id=telemetry.thread_id)
                    else:
                        receipt.update(credential_revoked=True, hook_events=list(telemetry.hook_events))
                except (TelemetryRefused, ClaudeTelemetryRefused, ClaudeHostRefused, KeyError, OverflowError):
                    token_usage = None
                result = {**result, "host_receipt": receipt, "monitor_evidence": evidence}
            if native_review or host is not None:
                sidecar = handle.stdout_path.parent / "supervisor-result.json"
                try:
                    evidence = _publish(sidecar.parent, sidecar.name, result)
                except FileExistsError:
                    retained = _read_evidence(sidecar)
                    if retained != _canonical(result) + b"\n":
                        raise SupervisorRefused("EVIDENCE_CHANGED") from None
                    evidence = {"locator": str(sidecar), "sha256": hashlib.sha256(retained).hexdigest()}
            handle.result = {**result, "evidence": evidence}
        else:
            if handle.process is None:
                raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
            returncode = self._wait_admitted(handle, timeout)
        if handle.result is None:
            streams = {}
            stream_bytes = {}
            for key, path in (("stdout", handle.stdout_path), ("stderr", handle.stderr_path)):
                raw = _read_evidence(path, handle.stream_identities[key])
                stream_bytes[key] = raw
                streams[key] = {"locator": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            result = {"intent_id": handle.intent_id, "activity_id": handle.activity_id,
                      "identity": asdict(handle.identity), "initial_head": handle.initial_head,
                      "returncode": returncode, "started_at": handle.started_at,
                      "finished_at": time.time(), "streams": streams}
            if handle.codex_material is not None:
                material = handle.codex_material
                try:
                    if os.path.lexists(material.auth_path):
                        raise TelemetryRefused("AUTH_REVOCATION_UNPROVEN")
                    telemetry = parse_codex_telemetry(stream_bytes["stdout"])
                    usage = dict(telemetry.token_usage)
                    # Cached input is a classification within input_tokens.
                    # Charge cache writes separately and every generated token;
                    # this is conservative without double-charging cached input.
                    token_usage = (
                        usage["input_tokens"] + usage["cache_write_input_tokens"]
                        + usage["output_tokens"] + usage["reasoning_output_tokens"]
                    )
                    result["host_receipt"] = {
                        "schema": "ffs.codex-invocation-receipt/v1",
                        "status": "complete",
                        "binary": dict(material.binary), "version": material.version,
                        "argv_sha256": hashlib.sha256(_canonical(material.argv)).hexdigest(),
                        "environment_sha256": hashlib.sha256(_canonical(material.environment)).hexdigest(),
                        "cwd": material.cwd, "model": material.model, "effort": material.effort,
                        "runtime_sha256": material.runtime_sha256,
                        "config_sha256": material.config_sha256, "attempt": material.attempt,
                        "auth_sha256": material.auth_sha256, "auth_revoked": True,
                        "exit_code": returncode, "thread_id": telemetry.thread_id,
                        "telemetry_sha256": telemetry.sha256,
                        "token_usage": usage,
                    }
                except (TelemetryRefused, KeyError, OverflowError):
                    token_usage = None
                    result["host_receipt"] = {
                        "schema": "ffs.codex-invocation-receipt/v1",
                        "runtime_sha256": material.runtime_sha256,
                        "attempt": material.attempt, "exit_code": returncode,
                        "status": "uncertain", "telemetry_sha256": streams["stdout"]["sha256"],
                    }
            elif handle.claude_material is not None:
                material = handle.claude_material
                try:
                    if os.path.lexists(material.credential_path):
                        raise ClaudeTelemetryRefused("AUTH_REVOCATION_UNPROVEN")
                    telemetry = parse_claude_telemetry(
                        stream_bytes["stdout"], requested_model=material.model,
                        expected_session_id=material.session_id, expected_version=material.version,
                    )
                    usage = dict(telemetry.token_usage)
                    token_usage = (
                        usage["input_tokens"] + usage["cache_creation_input_tokens"]
                        + usage["output_tokens"]
                    )
                    result["host_receipt"] = {
                        "schema": "ffs.claude-invocation-receipt/v1",
                        "status": "complete",
                        "binary": dict(material.binary), "version": material.version,
                        "argv_sha256": hashlib.sha256(_canonical(material.argv)).hexdigest(),
                        "environment_sha256": material.environment_sha256,
                        "cwd": material.cwd, "model": material.model, "effort": material.effort,
                        "session_id": material.session_id, "runtime_sha256": material.runtime_sha256,
                        "attempt": material.attempt, "credential_sha256": material.credential_sha256,
                        "credential_revoked": True, "exit_code": returncode,
                        "telemetry_sha256": telemetry.sha256,
                        "token_usage": usage, "hook_events": list(telemetry.hook_events),
                    }
                except (ClaudeHostRefused, ClaudeTelemetryRefused, KeyError, OverflowError):
                    token_usage = None
                    result["host_receipt"] = {
                        "schema": "ffs.claude-invocation-receipt/v1",
                        "runtime_sha256": material.runtime_sha256,
                        "attempt": material.attempt, "exit_code": returncode,
                        "status": "uncertain", "telemetry_sha256": streams["stdout"]["sha256"],
                    }
            elif handle.claude_qualification_material is not None:
                material = handle.claude_qualification_material
                try:
                    if material.probe_name == "auth-negative":
                        try:
                            auth = json.loads(stream_bytes["stdout"])
                        except (ValueError, UnicodeError) as error:
                            raise ClaudeTelemetryRefused("AUTH_NEGATIVE_INVALID") from error
                        if not (returncode != 0 or (isinstance(auth, dict) and auth.get("loggedIn") is False)):
                            raise ClaudeTelemetryRefused("AMBIENT_AUTH_REACHABLE")
                        usage, passed = {}, True
                    else:
                        if os.path.lexists(material.credential_path or ""):
                            raise ClaudeTelemetryRefused("AUTH_REVOCATION_UNPROVEN")
                        telemetry = parse_claude_telemetry(
                            stream_bytes["stdout"], requested_model=material.model,
                            expected_session_id=material.session_id or "", expected_version=material.version,
                        )
                        usage = dict(telemetry.token_usage)
                        passed = returncode == 0
                    token_usage = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                                   + usage.get("output_tokens", 0))
                    result["host_receipt"] = {
                        "schema": "ffs.claude-qualification-invocation/v1",
                        "probe_name": material.probe_name, "contract_sha256": material.contract_sha256,
                        "envelope_sha256": material.envelope_sha256,
                        "runtime_template_sha256": material.runtime_template_sha256,
                        "exit_code": returncode, "token_usage": usage, "passed": passed,
                        "telemetry_sha256": streams["stdout"]["sha256"],
                    }
                except (ClaudeHostRefused, ClaudeTelemetryRefused, KeyError, OverflowError):
                    token_usage = None
                    result["host_receipt"] = {
                        "schema": "ffs.claude-qualification-invocation/v1",
                        "probe_name": material.probe_name, "contract_sha256": material.contract_sha256,
                        "envelope_sha256": material.envelope_sha256, "exit_code": returncode,
                        "status": "uncertain", "telemetry_sha256": streams["stdout"]["sha256"],
                    }
            elif handle.qualification_material is not None:
                material = handle.qualification_material
                try:
                    telemetry = parse_codex_telemetry(stream_bytes["stdout"])
                    usage = dict(telemetry.token_usage)
                    token_usage = (
                        usage["input_tokens"] + usage["cache_write_input_tokens"]
                        + usage["output_tokens"] + usage["reasoning_output_tokens"]
                    )
                    result["host_receipt"] = {
                        "schema": "ffs.codex-qualification-invocation/v1",
                        "probe_name": material.probe_name,
                        "contract_sha256": material.contract_sha256,
                        "envelope_sha256": material.envelope_sha256,
                        "runtime_template_sha256": material.runtime_template_sha256,
                        "argv_sha256": hashlib.sha256(_canonical(material.argv)).hexdigest(),
                        "environment_sha256": hashlib.sha256(
                            _canonical(material.environment),
                        ).hexdigest(),
                        "exit_code": returncode, "thread_id": telemetry.thread_id,
                        "telemetry_sha256": telemetry.sha256, "token_usage": usage,
                    }
                except (TelemetryRefused, KeyError, OverflowError):
                    token_usage = None
                    result["host_receipt"] = {
                        "schema": "ffs.codex-qualification-invocation/v1",
                        "probe_name": material.probe_name,
                        "contract_sha256": material.contract_sha256,
                        "envelope_sha256": material.envelope_sha256,
                        "exit_code": returncode, "status": "uncertain",
                        "telemetry_sha256": streams["stdout"]["sha256"],
                    }
            elif handle.local_check_material is not None:
                material = handle.local_check_material
                token_usage = 0
                result["host_receipt"] = {
                    "schema": "ffs.local-check-invocation/v1",
                    "acceptance_hash": material.acceptance_hash, "check_id": material.check_id,
                    "material_sha256": material.material_sha256, "exit_code": returncode,
                    "token_usage": {"model_tokens": 0},
                }
            evidence = _publish(handle.stdout_path.parent, "result.json", result)
            handle.result = {**result, "evidence": evidence}
        else:
            evidence = handle.result["evidence"]
        valid_usage = type(token_usage) is int and 0 <= token_usage <= 9_223_372_036_854_775_807
        qualification_passed = bool(handle.result.get("host_receipt", {}).get("passed"))
        status = ("succeeded" if returncode == 0 or qualification_passed else "failed") if valid_usage else "uncertain"
        self.store.complete_launch(handle.intent_id, self.token, status=status,
                                   evidence=evidence, token_usage=token_usage if valid_usage else None,
                                   **self._policy_clock())
        reservation = self._shared_reservations.get(handle.intent_id)
        if reservation is not None:
            self.shared_resource_coordinator.release(reservation)
            self.shared_resource_coordinator.record_feedback(
                reservation, outcome="success" if status == "succeeded" else status,
            )
            # Kept until both settle: release and feedback are idempotent, so a replay retries them.
            self._shared_reservations.pop(handle.intent_id, None)
        handle.recorded = True
        return handle.result


def _guarded_codex_exec(command: list[str], environment: dict[str, str], guard: dict) -> int:
    """Forward one Codex stream only after revoking its runtime-owned auth copy."""
    if (not isinstance(guard, dict) or set(guard) != {"path", "sha256", "device", "inode"}
            or not isinstance(guard["path"], str)
            or not isinstance(guard["sha256"], str) or len(guard["sha256"]) != 64
            or type(guard["device"]) is not int or type(guard["inode"]) is not int):
        raise SupervisorRefused("AUTH_GUARD_INVALID")
    auth = Path(guard["path"])
    home = Path(environment.get("CODEX_HOME", ""))
    if (not auth.is_absolute() or auth.parent != home or auth.name != "auth.json"
            or environment.get("HOME") != str(home)):
        raise SupervisorRefused("AUTH_GUARD_INVALID")
    directory = os.open(str(home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    process = None
    revoked = False
    try:
        info = os.stat("auth.json", dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != (guard["device"], guard["inode"])):
            raise SupervisorRefused("AUTH_GUARD_CHANGED")
        if hashlib.sha256(_read_evidence(auth, (info.st_dev, info.st_ino))).hexdigest() != guard["sha256"]:
            raise SupervisorRefused("AUTH_GUARD_CHANGED")
        process = subprocess.Popen(
            command, cwd=os.getcwd(), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=None, bufsize=0,
        )
        assert process.stdout is not None
        for line in iter(process.stdout.readline, b""):
            if not revoked:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    record = None
                if isinstance(record, dict) and record.get("type") == "thread.started":
                    current = os.stat("auth.json", dir_fd=directory, follow_symlinks=False)
                    if ((current.st_dev, current.st_ino) != (guard["device"], guard["inode"])
                            or current.st_nlink != 1
                            or hashlib.sha256(_read_evidence(
                                auth, (current.st_dev, current.st_ino),
                            )).hexdigest() != guard["sha256"]):
                        process.terminate()
                        raise SupervisorRefused("AUTH_GUARD_CHANGED")
                    os.unlink("auth.json", dir_fd=directory)
                    os.fsync(directory)
                    revoked = True
            view = memoryview(line)
            while view:
                written = os.write(sys.stdout.fileno(), view)
                if written <= 0:
                    raise SupervisorRefused("EVIDENCE_CHANGED")
                view = view[written:]
        returncode = process.wait()
        if not revoked:
            raise SupervisorRefused("AUTH_REVOCATION_UNPROVEN")
        return returncode
    finally:
        os.close(directory)
        if process is not None and process.poll() is None:
            process.terminate()


def _guarded_claude_exec(command: list[str], environment: dict[str, str], guard: dict) -> int:
    """Keep Claude authenticated while removing its credential around tool execution."""
    required = {"path", "sha256", "device", "inode", "session_id", "model", "version"}
    if (
        not isinstance(guard, dict) or set(guard) != required
        or not isinstance(guard["path"], str) or not isinstance(guard["sha256"], str)
        or len(guard["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in guard["sha256"])
        or type(guard["device"]) is not int or type(guard["inode"]) is not int
        or any(not isinstance(guard[key], str) or not guard[key] for key in ("session_id", "model", "version"))
    ):
        raise SupervisorRefused("AUTH_GUARD_INVALID")
    credential = Path(guard["path"])
    home = Path(environment.get("CLAUDE_CONFIG_DIR", ""))
    if (
        not credential.is_absolute() or credential.parent != home or credential.name != ".credentials.json"
        or not home.is_absolute() or environment.get("HOME") != str(home.parent)
    ):
        raise SupervisorRefused("AUTH_GUARD_INVALID")
    directory = os.open(str(home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    process = None
    credential_bytes = b""
    credential_identity = (guard["device"], guard["inode"])
    credential_present = True
    authenticated_seen = False
    init_seen = False

    def validate_credential() -> os.stat_result:
        info = os.stat(".credentials.json", dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or (info.st_dev, info.st_ino) != credential_identity
            or hashlib.sha256(_read_evidence(credential, credential_identity)).hexdigest() != guard["sha256"]
        ):
            raise SupervisorRefused("AUTH_GUARD_CHANGED")
        return info

    def revoke_credential() -> None:
        nonlocal credential_present
        validate_credential()
        os.unlink(".credentials.json", dir_fd=directory)
        os.fsync(directory)
        credential_present = False

    def restore_credential() -> None:
        nonlocal credential_identity, credential_present
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(".credentials.json", flags, 0o600, dir_fd=directory)
        try:
            view = memoryview(credential_bytes)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise SupervisorRefused("AUTH_GUARD_CHANGED")
                view = view[written:]
            os.fsync(fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            ):
                raise SupervisorRefused("AUTH_GUARD_CHANGED")
            credential_identity = (info.st_dev, info.st_ino)
        except BaseException:
            try:
                os.unlink(".credentials.json", dir_fd=directory)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        os.fsync(directory)
        credential_present = True

    def has_content(record: object, kind: str) -> bool:
        if not isinstance(record, dict):
            return False
        message = record.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            return False
        return any(isinstance(item, dict) and item.get("type") == kind for item in message["content"])

    try:
        validate_credential()
        credential_bytes = _read_evidence(credential, credential_identity)
        process = subprocess.Popen(
            command, cwd=os.getcwd(), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=None, bufsize=0,
        )
        assert process.stdout is not None
        for line in iter(process.stdout.readline, b""):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                record = None
            if isinstance(record, dict) and record.get("type") == "system" and record.get("subtype") == "init":
                if (
                    record.get("session_id") != guard["session_id"]
                    or record.get("model") != guard["model"]
                    or record.get("claude_code_version") != guard["version"]
                ):
                    process.terminate()
                    raise SupervisorRefused("AUTH_GUARD_CHANGED")
                init_seen = True
            elif isinstance(record, dict) and record.get("type") == "assistant" and init_seen:
                authenticated_seen = True
                if credential_present:
                    # Claude reloads subscription credentials for every model
                    # turn. Remove the private copy at the first authenticated
                    # output, before a later streamed tool request can execute,
                    # then restore it only after a retained tool result so the
                    # next authenticated turn can start.
                    revoke_credential()
            elif (
                isinstance(record, dict) and record.get("type") == "user"
                and not credential_present and has_content(record, "tool_result")
            ):
                restore_credential()
            view = memoryview(line)
            while view:
                written = os.write(sys.stdout.fileno(), view)
                if written <= 0:
                    raise SupervisorRefused("EVIDENCE_CHANGED")
                view = view[written:]
        returncode = process.wait()
        if not authenticated_seen:
            raise SupervisorRefused("AUTH_REVOCATION_UNPROVEN")
        if credential_present:
            revoke_credential()
        return returncode
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if credential_present:
            try:
                revoke_credential()
            except (OSError, SupervisorRefused):
                pass
        os.close(directory)


def _child(fd: int, *, native_review: bool = False) -> int:
    channel = socket.socket(fileno=fd)
    channel.settimeout(30)
    try:
        request = _receive(channel, max_bytes=_bootstrap_frame_limit(
            native_review=native_review,
        ))
        if Path.cwd().resolve() != Path(request["workspace"]):
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        identity = _workspace_identity(Path.cwd())
        if identity != request["workspace_identity"]:
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        initial_head = _head(Path.cwd())
        if initial_head != request["expected_head"]:
            raise SupervisorRefused("FORK_BASE_MISMATCH")
        _send(channel, {"intent_id": request["intent_id"],
                        "identity": asdict(ProcessIdentity.current()), "initial_head": initial_head,
                        "workspace_identity": identity})
        permit = _receive(channel)
        if (permit.get("authorized") is not True or not permit.get("permit_id")
                or permit.get("intent_id") != request["intent_id"]
                or permit.get("generation") != request["generation"]):
            raise SupervisorRefused("FENCE_REVOKED")
        command = request["command"]
        current = os.stat(".")
        if ([current.st_dev, current.st_ino] != request["workspace_identity"]
                or _workspace_identity(Path(request["workspace"])) != request["workspace_identity"]
                or _head(Path.cwd()) != request["expected_head"]):
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        material = request.get("host_material")
        launch_environment = request.get("launch_environment")
        if material is not None and launch_environment is not None:
            raise SupervisorRefused("HOST_MATERIAL_INVALID")
        if material is None and launch_environment is None:
            # Preserve existing admitted generic-process behavior.  The closed
            # environment below is reserved for a future qualified host adapter.
            environment = os.environ.copy()
        elif material is not None:
            if (not isinstance(material, dict)
                    or not isinstance(material.get("FFS_ARTIFACT_REVIEW_PROMPT"), str)):
                raise SupervisorRefused("HOST_MATERIAL_INVALID")
            environment = material
            if not all(isinstance(key, str) and key and "=" not in key and "\0" not in key
                       and isinstance(value, str) and "\0" not in value
                       for key, value in environment.items()):
                raise SupervisorRefused("HOST_MATERIAL_INVALID")
        else:
            environment = launch_environment
            if (not isinstance(environment, dict) or not environment
                    or not all(isinstance(key, str) and key and "=" not in key and "\0" not in key
                               and isinstance(value, str) and "\0" not in value
                               for key, value in environment.items())):
                raise SupervisorRefused("HOST_MATERIAL_INVALID")
        if "worker_endpoint" in permit:
            environment["FFS_WORKER_ENDPOINT"] = permit["worker_endpoint"]
            environment["FFS_WORKER_SCOPE"] = _canonical(permit["worker_scope"]).decode()
        if "worker_file_channel" in permit:
            environment["FFS_WORKER_FILE_CHANNEL"] = _canonical(
                permit["worker_file_channel"],
            ).decode()
        auth_guard = request.get("auth_guard")
        claude_auth_guard = request.get("claude_auth_guard")
        if auth_guard is not None and claude_auth_guard is not None:
            raise SupervisorRefused("AUTH_GUARD_INVALID")
        channel.close()
        if auth_guard is not None:
            return _guarded_codex_exec(command, environment, auth_guard)
        if claude_auth_guard is not None:
            return _guarded_claude_exec(command, environment, claude_auth_guard)
        os.execve(command[0], command, environment)
    finally:
        channel.close()
    return 70


def _monitor(fd: int, *, native_review: bool = False) -> int:
    """Wait for one permitted native child and publish its immutable receipt."""
    channel = socket.socket(fileno=fd)
    child_channel = None
    proc = None
    try:
        bootstrap_limit = _bootstrap_frame_limit(native_review=native_review)
        request = _receive(channel, max_bytes=bootstrap_limit)
        required = {"intent_id", "activity_id", "generation", "command", "workspace", "expected_head",
                    "workspace_identity", "evidence_root", "streams"}
        optional = {"launch_environment", "auth_guard", "claude_auth_guard"}
        if (not required.issubset(request) or set(request) - required - optional
                or Path.cwd().resolve() != Path(request["workspace"])):
            raise SupervisorRefused("MONITOR_PROTOCOL_INVALID")
        child_channel, native_channel = socket.socketpair()
        env = os.environ.copy()
        proc = subprocess.Popen(
            [sys.executable, "-m", "run_state.supervisor",
             "_child_native" if native_review else "_child", str(native_channel.fileno())],
            cwd=request["workspace"], env=env, pass_fds=(native_channel.fileno(),),
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        native_channel.close()
        _send(child_channel, {key: value for key, value in request.items()
                              if key not in {"activity_id", "evidence_root", "streams"}},
              max_bytes=bootstrap_limit)
        reply = _receive(child_channel)
        native_identity = ProcessIdentity(**reply["identity"])
        if native_identity.pid != proc.pid:
            raise SupervisorRefused("CHILD_IDENTITY_MISMATCH")
        _send(channel, {"intent_id": request["intent_id"], "monitor": asdict(ProcessIdentity.current()),
                        "native": asdict(native_identity), "initial_head": reply["initial_head"],
                        "workspace_identity": reply["workspace_identity"]})
        permit = _receive(channel)
        _send(child_channel, permit)
        child_channel.close()
        released_at = time.time()
        returncode = proc.wait()
        guard = request.get("auth_guard") or request.get("claude_auth_guard")
        auth_revoked = guard is not None and not os.path.lexists(guard["path"])
        streams = {}
        for key, name in (("stdout", "stdout.log"), ("stderr", "stderr.log")):
            path = Path(request["evidence_root"]) / name
            expected = tuple(request["streams"].get(key, ()))
            if len(expected) != 2:
                raise SupervisorRefused("EVIDENCE_CHANGED")
            raw = _read_evidence(path, expected)
            streams[key] = {"locator": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        _publish(Path(request["evidence_root"]), "result.json", {
            "intent_id": request["intent_id"], "activity_id": request["activity_id"],
            "identity": asdict(native_identity), "initial_head": reply["initial_head"],
            "returncode": returncode, "started_at": released_at, "finished_at": time.time(), "streams": streams,
            "auth_revoked": auth_revoked,
        })
        return 0
    finally:
        channel.close()
        if child_channel is not None:
            child_channel.close()


def finish_owned_wave_client(supervisor, handle, wave_consumer, *, timeout: float) -> dict:
    """Settle an outer client's launch only after its claimed work finishes."""
    deadline = time.monotonic() + timeout
    policy_deadline = False
    try:
        if handle.process is not None:
            supervisor._wait_admitted(handle, timeout)
        remaining, policy_deadline = supervisor._policy_timeout(max(0.0, deadline - time.monotonic()))
        wave_consumer.wait_for_idle(intent_id=handle.intent_id, timeout=remaining)
    except OwnershipRefused:
        raise
    except ControlStoreRefused as error:
        try:
            supervisor.expire_launch(handle, reason="wave settlement authority refused: " + error.code)
        except ControlStoreRefused:
            # Preserve the typed authority refusal if containment itself must
            # await store reconciliation; never fabricate terminal settlement.
            pass
        raise SupervisorRefused(error.code) from error
    except SupervisorRefused as error:
        if error.code == "ACTIVE_TIME_EXHAUSTED":
            raise
        if policy_deadline and error.code == "WAVE_SETTLEMENT_TIMEOUT":
            supervisor.expire_policy_run(reason="ACTIVE_TIME_EXHAUSTED")
            raise SupervisorRefused("ACTIVE_TIME_EXHAUSTED") from None
        supervisor.expire_launch(handle, reason="supervisor-owned wave deadline exceeded")
        raise SupervisorRefused("WAVE_SETTLEMENT_TIMEOUT") from None
    except subprocess.TimeoutExpired:
        supervisor.expire_launch(handle, reason="supervisor-owned wave deadline exceeded")
        raise SupervisorRefused("WAVE_SETTLEMENT_TIMEOUT") from None
    return supervisor.finish(handle, timeout=timeout)


def _gsd_wave_completion_code(
    store, activity_id: str, intent_id: str, *, require_wave: bool = True,
) -> str | None:
    """Return the typed refusal for an unproven direct execute completion."""
    with store.read_transaction() as tx:
        wave_events = tx.execute(
            "SELECT e.id FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
            "WHERE k.activity_id=? AND k.idempotency_key LIKE ? AND e.event_type=k.idempotency_key",
            (activity_id, "worker-request:" + intent_id + ":gsd-wave:%"),
        ).fetchall()
        replies = [tx.execute(
            "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
            "WHERE k.activity_id=? AND k.idempotency_key=?",
            (activity_id, f"gsd-wave:{event['id']}:reply"),
        ).fetchone() for event in wave_events]
        refusals = [tx.execute(
            "SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
            (activity_id, f"gsd-wave:{event['id']}:refused"),
        ).fetchone() for event in wave_events]
    if any(refusal is not None and reply is None for refusal, reply in zip(refusals, replies, strict=True)):
        return "WAVE_EXECUTION_REFUSED"
    if not wave_events:
        return "WAVE_EXECUTION_UNPROVEN" if require_wave else None
    if not all(item is not None for item in replies):
        return "WAVE_EXECUTION_UNPROVEN"
    for row in replies:
        try:
            data = json.loads(row["payload"])["data"]
            raw = _read_evidence(Path(data["evidence"]["locator"]))
            reply = data["reply"]
            if (hashlib.sha256(raw).hexdigest() != data["evidence"]["sha256"]
                    or json.loads(raw) != reply or not reply["results"]):
                return "WAVE_EXECUTION_UNPROVEN"
            if any(item["status"] != "complete" for item in reply["results"]):
                return "WAVE_EXECUTION_REFUSED"
        except (OSError, KeyError, TypeError, ValueError, SupervisorRefused):
            return "WAVE_EXECUTION_UNPROVEN"
    return None


def _managed_command_requires_wave_proof(invocation: tuple[str, ...]) -> bool | None:
    if invocation and invocation[0] in {"/gsd-execute-phase", "feature-implement", "task-swarm"}:
        return True
    if invocation and invocation[0] in {"feature-spec", "fix", "code-uplift"}:
        return False
    return None


def _managed_gsd_prompt(prompt_command: str) -> str:
    return (
        prompt_command
        + "\n\nThis is an FFS managed recovery workspace. Preserve the no-commit rule: "
          "do not stage, commit, merge, push, release, deploy, or change active profiles. "
          "Return scoped patch and evidence outputs through the FFS supervisor. "
          "Do not start unmanaged agents or grandchildren. "
          "For every GSD executor wave, the FFS-supervised-process compatibility path is mandatory: "
          "read its section in executor-isolation-dispatch.md, build one complete wave manifest, "
          "and invoke ffs-supervised-dispatch.cjs. The outer orchestrator must never edit a plan's "
          "declared target files itself or use GSD's inline/sequential fallback. If the adapter cannot "
          "run, report the capability failure without performing plan work. If the adapter command "
          "returns an in-progress session, poll that same session with empty write_stdin calls until "
          "it exits. Do not poll its output path, start another shell, or end the turn while the "
          "adapter session is still running. The supervisor applies accepted patches before replying; "
          "do not apply them again. Treat .ffs-observer-tmp, .planning/.ffs-worker-channel, and "
          ".planning/.ffs-wave-requests as supervisor-owned: do not list, read, write, or summarize "
          "their contents."
    )


def _managed_wave_prompt(plan_prompt: str) -> str:
    return (
        "FFS supervised patch-only executor. Execute only the assigned plan in this registered workspace. "
        "Do not git add, commit, amend, cherry-pick, merge, push, release, deploy, or launch agents. "
        "The supervisor harvests your scoped patch and publishes the plan completion receipt; "
        "a git commit or committed SUMMARY is neither required nor authorized. "
        "Skip upstream GSD commit and metadata-commit steps. Return evidence for the assigned checks.\n\n"
        + plan_prompt
    )


def run_managed_command(store, token, context, command, request_key,
                        dispatch_limit, token_limit, host_request=None, upstream_runtime=None, *,
                        model_request=None, review_catalog=None, acceptance_draft=None) -> int:
    """Production context callback for explicitly selected local processes.

    Every native process, including qualification and nested GSD executors,
    crosses the same durable launch-intent and authorization boundary.  With a
    sealed acceptance (or an explicit draft to seal after outer qualification)
    the run is driven through the frontend lifecycle producers; otherwise the
    outer orchestrator executes once, as before.
    """
    if host_request is None:
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    from run_state.frontend_producers import terminal_lifecycle_outcome
    terminal = terminal_lifecycle_outcome(store, token)
    if terminal is not None:
        # A terminal sealed lifecycle replays without re-preparing or relaunching anything.
        return terminal
    from run_state.host_request import ClaudeHostRequest, CodexHostRequest
    if type(host_request) is ClaudeHostRequest:
        from run_state.managed_claude_qualification import run_managed_claude_command
        return run_managed_claude_command(store, token, context, command, request_key, host_request,
                                         upstream_runtime=upstream_runtime, model_request=model_request,
                                         acceptance_draft=acceptance_draft)
    if type(host_request) is not CodexHostRequest:
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    from run_state.frontend_producers import drive_managed_session
    session = prepare_managed_codex_session(
        store, token, context, command, request_key, host_request, upstream_runtime=upstream_runtime,
        model_request=model_request, review_catalog=review_catalog)
    return drive_managed_session(store, token, context, session, acceptance_draft=acceptance_draft)


def _managed_prompt(root, operation, command) -> tuple[tuple[str, ...], str, str]:
    """Return ``(invocation, prompt, role)`` for a managed host command."""
    invocation = tuple(command)
    if len(invocation) == 1 and invocation[0] in {
        "feature-spec", "feature-implement", "fix", "code-uplift", "task-swarm",
    }:
        invocation_text = ""
        if operation is not None:
            try:
                invocation_text = json.loads(operation)["data"]["invocation_text"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise SupervisorRefused("MANAGED_COMMAND_CONTEXT_CONFLICT") from None
        prompt_command = "$" + invocation[0]
        if invocation_text:
            prompt_command += " " + invocation_text
    else:
        head = invocation[0]
        prompt_command = ("$" + head[1:] if head.startswith("/") else head) + (
            " " + " ".join(invocation[1:]) if len(invocation) > 1 else ""
        )
    return invocation, _managed_gsd_prompt(prompt_command), "reviewer" if root["kind"] == "review" else "worker"


def _managed_inventory_workspace(store, token, context, request_key, *, workspace_api=None):
    """Reconstruct the exact retained root snapshot into a registered inventory child.

    ``workspace_api`` lets a host module supply its own bound workspace seams
    (the Claude module keeps its historical patch points); the default is this
    module's own imports.
    """
    api = workspace_api or {}
    from_row = api.get("_from_row", _from_row)
    load_snapshot = api.get("load_input_snapshot", load_input_snapshot)
    verify_complete = api.get("_verify_snapshot_complete", _verify_snapshot_complete)
    begin_child = api.get("begin_child_workspace_preparation", begin_child_workspace_preparation)
    prepare = api.get("prepare_workspace", prepare_workspace)
    inspect = api.get("inspect_workspace", inspect_workspace)
    with productive_work(store, token, kind="preparation"):
        with store.read_transaction() as tx:
            root = tx.execute(
                "SELECT w.*,a.kind FROM context_runs r JOIN context_workspaces w "
                "ON w.preparation_id=r.preparation_id JOIN authority_activities a "
                "ON a.id=r.activity_id WHERE r.repository_id=? AND r.run_id=? "
                "AND r.activity_id=?",
                (token.repository_id, token.run_id, context.activity_id),
            ).fetchone()
            operation = tx.execute(
                "SELECT e.payload FROM authority_event_keys k JOIN control_events e ON e.id=k.event_id "
                "WHERE k.activity_id=? AND k.idempotency_key='frontend-operation'",
                (context.activity_id,),
            ).fetchone()
        if root is None or root["state"] != "ready":
            raise SupervisorRefused("WORKSPACE_BINDING_MISMATCH")
        parent = from_row(root)
        snapshot = load_snapshot(store, parent)
        if snapshot is None:
            raise SupervisorRefused("INPUT_SELECTION_CHANGED")
        verify_complete(store, parent)
        child_key = "managed-host:" + request_key
        with store.read_transaction() as tx:
            retained = tx.execute(
                "SELECT preparation_id FROM context_workspaces WHERE repository_id=? AND run_id=? "
                "AND child_request_key=?", (token.repository_id, token.run_id, child_key)).fetchone()
        if retained is not None:
            # Replay: promotion already rewrote the retained preparation's role to
            # the admitted worker/reviewer, so a fresh inventory request would no
            # longer match it.  Reuse the journaled preparation as it stands.
            pending = inspect(store, retained["preparation_id"])
        else:
            pending = begin_child(
                store, token, parent_activity_id=context.activity_id, request_key=child_key,
                role="inventory",
                base_commit=parent.base_commit, selected_input_manifest=snapshot.manifest,
                repository_path=parent.repository_path,
            )
        ready = pending if pending.ready else prepare(store, token, pending, input_snapshot=snapshot)
        if not ready.ready:
            raise SupervisorRefused("WORKSPACE_RECONCILIATION_REQUIRED")
    return root, None if operation is None else (operation["payload"] if "payload" in operation.keys() else None), child_key, ready


def _replayed_launch_refusal(launch) -> str:
    """The refusal for a replay that reaches a real (non-qualification) launch intent.

    A settled intent, including ``closed_dead``, may have run its work, so it is
    reported and never repeated; an unsettled one needs owner-fence reconciliation.
    """
    return ("REQUEST_ALREADY_COMPLETED"
            if launch["state"] in {"completed_succeeded", "completed_failed", "closed_dead"}
            else "INTENT_RECONCILIATION_REQUIRED")


def _retained_runtime_refusal(launch, *, outer: bool) -> str:
    """The refusal for a retained private runtime that cannot be resumed."""
    if launch is not None:
        return _replayed_launch_refusal(launch)
    # Only the outer child's runtime is named by the request key.  A new key starts a new
    # outer run, so it never repairs a wave child or the final reviewer.
    return "RETAINED_RUNTIME_NOT_REUSABLE" if outer else "CHILD_RUNTIME_NOT_REUSABLE"


def prepare_managed_codex_session(store, token, context, command, request_key, host_request, *,
                                  upstream_runtime=None, model_request=None, review_catalog=None):
    """Qualification seams, worker channel and outer contract for one Codex host run."""
    from host_capabilities import GsdSupervisorEnvironment, admit_cli
    from run_state.codex_host import CodexHostAdapter
    from run_state.frontend_producers import (
        HostRuntimeSeam, ManagedHostSession, QualifiedHostRuntime, retained_launch, retained_outer_activity,
    )
    from run_state.managed_qualification import (
        ManagedQualificationRefused, qualify_managed_runtime,
    )
    from run_state.runtime_staging import (
        STAGE_MANIFEST_NAME, RetainedRuntimeNotReusable, stage_or_reuse_private_codex_runtime,
    )
    from run_state.wave_consumer import WaveConsumer
    from run_state.worker_channel import WorkerChannelServer
    import tempfile

    root, operation, child_key, ready = _managed_inventory_workspace(store, token, context, request_key)
    invocation, prompt, role = _managed_prompt(root, operation, command)
    socket_root = Path(tempfile.mkdtemp(prefix="ffs-worker-", dir="/tmp")).resolve()
    channel = WorkerChannelServer(store, token, socket_root / "worker.sock")
    host_evidence = Path(context.evidence_root) / "host"
    runtime_root = host_evidence / "runtimes"
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_root.chmod(0o700)
    supervisor = Supervisor(
        store, token, evidence_root=host_evidence,
        worker_channel=channel,
    )
    try:
        cli = admit_cli(host_request.binary)
    except (CapabilityError, OSError, ValueError) as error:
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED") from error
    bridge = Path(__file__).with_name("gsd_wave_bridge.py").resolve()
    if bridge.is_symlink() or not bridge.is_file():
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED")
    bridge_command = json.dumps(
        [sys.executable, str(bridge)], ensure_ascii=True, separators=(",", ":"),
    )

    def qualify_runtime(activity_id: str, preparation, activity_request_key: str,
                        parent_activity_id: str, final_contract_hash: str, child_role: str):
        """Stage one private runtime and qualify it; replay reads the retained observation."""
        private_home = runtime_root / activity_id
        try:
            with productive_work(store, token, kind="preparation"):
                stage_or_reuse_private_codex_runtime(
                    Path(host_request.runtime_home), private_home, preparation.path,
                )
            staged_request = replace(host_request, runtime_home=str(private_home))
            additions = GsdSupervisorEnvironment(
                "ffs-supervised-process", "patches",
                str(private_home / "supervisor-admission.json"), bridge_command,
            )
            bundle = qualify_managed_runtime(
                store, token, activity_id=activity_id,
                activity_request_key=activity_request_key,
                parent_activity_id=parent_activity_id, workspace=preparation,
                runtime_home=private_home, binary=Path(host_request.binary),
                gsd_environment=additions, host_request=staged_request,
                role=child_role, evidence_root=host_evidence,
                final_contract_hash=final_contract_hash, supervisor=supervisor,
            )
            adapter = CodexHostAdapter(
                bundle.qualified_runtime, host_request.binary, str(cli["version"]),
            )
        except RetainedRuntimeNotReusable as error:
            raise SupervisorRefused(_retained_runtime_refusal(
                retained_launch(store, activity_id), outer=activity_request_key == child_key)) from error
        except (CapabilityError, ManagedQualificationRefused, OSError, ValueError) as error:
            raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED") from error
        return QualifiedHostRuntime(bundle.activity, bundle.qualified_runtime, bundle.runtime_receipt,
                                    adapter, additions)

    def bind_launch(qualified, child_prompt: str, preparation, final_contract_hash: str, launch_request_key: str):
        """Bind one prompt to a qualified runtime's immutable launch material."""
        try:
            launch_material = qualified.adapter.build_launch_material(
                child_prompt, attempt=1, gsd_environment=qualified.additions,
            )
        except (CapabilityError, OSError, ValueError) as error:
            raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED") from error
        return DispatchRequest(
            activity_id=qualified.activity.id, request_key=launch_request_key,
            command=launch_material.argv, workspace=str(preparation.path),
            expected_head=preparation.base_commit,
            runtime_identity=store.runtime_tuple_hash(qualified.qualified),
            token_reservation=host_request.token_reservation,
            contract_hash=final_contract_hash, codex_material=launch_material,
            runtime_receipt_sha256=qualified.receipt.receipt_sha256,
            managed_input_sha256=preparation.input_digest,
        ), qualified.adapter

    def prepare_runtime(activity_id: str, preparation, activity_request_key: str,
                        parent_activity_id: str, final_contract_hash: str,
                        child_role: str, child_prompt: str, launch_request_key: str):
        qualified = qualify_runtime(activity_id, preparation, activity_request_key, parent_activity_id,
                                    final_contract_hash, child_role)
        return bind_launch(qualified, child_prompt, preparation, final_contract_hash, launch_request_key)

    outer_activity_id = (retained_outer_activity(store, token, parent_activity_id=context.activity_id,
                                                 child_key=child_key) or str(uuid.uuid4()))
    outer_home = runtime_root / outer_activity_id
    launch = retained_launch(store, outer_activity_id)
    if launch is not None:
        # A real outer launch holds Codex state in its home and may have done work:
        # never re-stage, re-qualify, relaunch or replay it as a success.  Resuming
        # after a real launch would first need its wave proof checked again.
        raise SupervisorRefused(_replayed_launch_refusal(launch))
    try:
        with productive_work(store, token, kind="preparation"):
            stage_or_reuse_private_codex_runtime(
                Path(host_request.runtime_home), outer_home, ready.path,
            )
    except RetainedRuntimeNotReusable as error:
        # Only qualification consumed the outer stage; this request key cannot resume it.
        raise SupervisorRefused("RETAINED_RUNTIME_NOT_REUSABLE") from error
    except (CapabilityError, OSError, ValueError) as error:
        raise SupervisorRefused("HOST_CAPABILITY_UNQUALIFIED") from error
    contract_material = {
        "schema": "ffs.managed-codex-contract/v2", "command": list(invocation),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "host_request": host_request.material(), "input_digest": ready.input_digest,
        "runtime_stage_sha256": hashlib.sha256(
            (outer_home / STAGE_MANIFEST_NAME).read_bytes(),
        ).hexdigest(),
        "gsd_bridge_sha256": hashlib.sha256(bridge.read_bytes()).hexdigest(),
    }
    contract_hash = hashlib.sha256(_canonical(contract_material)).hexdigest()

    def prepare_wave_child(wave_context):
        request, _adapter = prepare_runtime(
            wave_context.activity_id, wave_context.preparation,
            wave_context.request_key, wave_context.parent_activity_id,
            wave_context.contract_hash, "worker", _managed_wave_prompt(wave_context.plan["prompt"]),
            wave_context.request_key,
        )
        return request

    channel.start()
    wave_consumer = WaveConsumer(
        supervisor, prepare_wave_child, finish_timeout=host_request.timeout_seconds,
    )
    channel.attach_wave_consumer(wave_consumer)

    def prepare_outer():
        return prepare_runtime(
            outer_activity_id, ready, child_key, context.activity_id,
            contract_hash, role, prompt, child_key + ":launch",
        )

    def execute(request, _adapter, *, settle_success=True):
        child = store.get_activity(request.activity_id)
        if upstream_runtime is not None:
            # Promotion rewrote the inventory workspace's child_role to the
            # admitted role; the prelaunch capture compares the live row to
            # the preparation it is given, so re-read it rather than pass the
            # stale pre-qualification copy.
            admitted = inspect_workspace(store, ready.id)
            supervisor.configure_managed_parent_resources(request, context, admitted, upstream_runtime)
        handle = supervisor.launch_managed_outer(request)
        # Process exit is only a client observation.  Leave its durable
        # execution intent intact until the claimed wave publishes its result;
        # finish() settles that intent and would otherwise revoke its children
        # while the independent channel thread still owns their execution.
        result = finish_owned_wave_client(
            supervisor, handle, wave_consumer, timeout=host_request.timeout_seconds,
        )
        host_receipt = result.get("host_receipt", {})
        if host_receipt.get("status") == "uncertain":
            store.transition_activity(
                token, child.id, expected="active", new="paused",
                reason="qualified host telemetry is uncertain",
            )
            raise SupervisorRefused("MALFORMED_TELEMETRY")
        wave_required = _managed_command_requires_wave_proof(invocation)
        if result["returncode"] == 0 and wave_required is not None:
            wave_refusal = _gsd_wave_completion_code(
                store, child.id, handle.intent_id, require_wave=wave_required,
            )
            if wave_refusal == "WAVE_EXECUTION_REFUSED":
                store.transition_activity(
                    token, child.id, expected="active", new="failed",
                    result=result["evidence"], reason="GSD wave execution was refused",
                )
                raise SupervisorRefused("WAVE_EXECUTION_REFUSED")
            if wave_refusal is not None:
                store.transition_activity(
                    token, child.id, expected="active", new="failed",
                    result=result["evidence"], reason="GSD execution returned without supervised wave evidence",
                )
                raise SupervisorRefused("WAVE_EXECUTION_UNPROVEN")
        if result["returncode"] == 0:
            # Under the sealed lifecycle the outer activity stays active for the
            # mapped checks and the final review; ``settle`` closes it before DONE.
            if settle_success:
                store.transition_activity(
                    token, child.id, expected="active", new="succeeded",
                    result=result["evidence"], reason="qualified host process completed",
                )
        else:
            store.transition_activity(
                token, child.id, expected="active", new="failed",
                result=result["evidence"], reason="qualified host process failed",
            )
        return result["returncode"], handle, result

    def close(handle, adapter, material):
        channel.close()
        supervisor.contain_revoked()
        try:
            socket_root.rmdir()
        except OSError:
            pass
        if (adapter is not None and material is not None and handle is not None
                and handle.process is not None and handle.process.poll() is not None):
            try:
                adapter.release_launch_material(material)
            except Exception:
                # The verified directory is retained for finalization review.
                pass

    catalog_path = catalog_sha256 = None
    if review_catalog is not None:
        catalog_path, catalog_sha256 = review_catalog
    seam = HostRuntimeSeam(
        host="codex", qualify=qualify_runtime, bind=bind_launch, binary=host_request.binary,
        cli_version=str(cli["version"]), model=host_request.model, effort=host_request.effort,
        model_request=dict(model_request) if model_request is not None else {"kind": "exact", "id": host_request.model},
        catalog_path=catalog_path, catalog_sha256=catalog_sha256,
    )
    return ManagedHostSession(
        host="codex", supervisor=supervisor, evidence_root=host_evidence, ready=ready, child_key=child_key,
        outer_activity_id=outer_activity_id, invocation=invocation, timeout_seconds=host_request.timeout_seconds, seam=seam,
        prepare_outer=prepare_outer, execute=execute, close=close,
    )

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"_child", "_child_native", "_monitor", "_monitor_native"}:
        raise SystemExit("supervisor child is an internal inherited-channel entrypoint")
    try:
        entrypoint, fd = sys.argv[1], int(sys.argv[2])
        if entrypoint == "_child":
            result = _child(fd)
        elif entrypoint == "_child_native":
            result = _child(fd, native_review=True)
        elif entrypoint == "_monitor":
            result = _monitor(fd)
        else:
            result = _monitor(fd, native_review=True)
        raise SystemExit(result)
    except (SupervisorRefused, OwnershipRefused, OSError, ValueError, KeyError) as error:
        print(getattr(error, "code", "CHILD_PROTOCOL_FAILED"), file=sys.stderr)
        raise SystemExit(78) from error
