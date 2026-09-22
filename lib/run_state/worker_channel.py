"""Peer-authenticated worker requests to the live ControlStore supervisor.

This channel admits requests, not native agent launches or evidence acceptance.
Host confinement must separately deny workers direct control-store writes and
unmanaged spawning; same-user Unix credentials alone cannot provide that.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import hmac
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

from process_identity import LIVE, ProcessIdentity, probe_direct_parent, probe_identity
from .ownership import OwnershipRefused, assert_owner
from .state import ControlStoreRefused


class WorkerChannelRefused(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


_MAX_WAVE_MANIFEST_BYTES = 16 * 1024
_MAX_WAVE_REPLY_BYTES = 16 * 1024 * 1024
_MAX_WAVE_PLANS = 64
_MAX_WAVE_PROMPT_BYTES = 8 * 1024
_FILE_CHANNEL_DIRECTORY = ".ffs-worker-channel"


def _no_duplicate_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _safe_relative(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > 512:
        return None
    result = []
    for item in value:
        relative = PurePosixPath(item) if isinstance(item, str) else None
        if (relative is None or relative.is_absolute() or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or "\\" in item or "\0" in item or len(item.encode()) > 512):
            return None
        result.append(item)
    return tuple(result)


def parse_gsd_wave_manifest(raw: bytes) -> tuple[dict, bytes]:
    """Parse the bounded, canonical GSD wave evidence retained by the channel."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= _MAX_WAVE_MANIFEST_BYTES:
        raise WorkerChannelRefused("IPC_WAVE_MANIFEST_INVALID")
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
        canonical = _canonical(manifest)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as error:
        raise WorkerChannelRefused("IPC_WAVE_MANIFEST_INVALID") from error
    fields = {"schema", "mode", "phase", "wave", "initial_head", "commit_mode",
              "apply_between_waves", "orchestrator_root", "admission", "plans"}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest["schema"] != "ffs.gsd-supervised-dispatch/v1"
            or manifest["mode"] != "ffs-supervised-process"
            or not isinstance(manifest["phase"], str) or not 1 <= len(manifest["phase"].encode()) <= 128
            or isinstance(manifest["wave"], bool) or not isinstance(manifest["wave"], int)
            or not 1 <= manifest["wave"] <= 1_000_000
            or not isinstance(manifest["initial_head"], str)
            or re.fullmatch(r"[0-9a-f]{40}", manifest["initial_head"]) is None
            or manifest["commit_mode"] not in {"patches", "fixture-commits"}
            or manifest["apply_between_waves"] is not True
            or not isinstance(manifest["orchestrator_root"], str)
            or not Path(manifest["orchestrator_root"]).is_absolute()
            or "\0" in manifest["orchestrator_root"] or len(manifest["orchestrator_root"].encode()) > 4096
            or not isinstance(manifest["admission"], dict)
            or set(manifest["admission"]) != {"schema", "available", "repository_id", "run_id",
                                                   "activity_id", "generation", "workspace", "runtime_identity"}
            or manifest["admission"].get("schema") != "ffs.supervisor-admission/v1"
            or manifest["admission"].get("available") is not True
            or isinstance(manifest["admission"].get("generation"), bool)
            or not isinstance(manifest["admission"].get("generation"), int)
            or manifest["admission"]["generation"] < 1
            or any(not isinstance(manifest["admission"].get(key), str)
                   or not manifest["admission"][key] or "\0" in manifest["admission"][key]
                   for key in ("repository_id", "run_id", "activity_id", "workspace", "runtime_identity"))
            or not Path(manifest["admission"]["workspace"]).is_absolute()
            or not isinstance(manifest["plans"], list)
            or not 1 <= len(manifest["plans"]) <= _MAX_WAVE_PLANS
            or raw != canonical):
        raise WorkerChannelRefused("IPC_WAVE_MANIFEST_INVALID")
    plan_ids = set()
    for plan in manifest["plans"]:
        if not isinstance(plan, dict) or set(plan) not in (
                {"id", "initial_head", "prompt", "prompt_fresh", "prompt_nonce", "prompt_sha256",
                 "files_modified", "files_deleted", "depends_on"},
                {"id", "initial_head", "prompt", "prompt_fresh", "prompt_nonce", "prompt_sha256",
                 "files_modified", "files_deleted"}):
            raise WorkerChannelRefused("IPC_WAVE_MANIFEST_INVALID")
        plan_id, prompt = plan.get("id"), plan.get("prompt")
        if (not isinstance(plan_id, str) or not 1 <= len(plan_id.encode()) <= 128 or plan_id in plan_ids
                or not isinstance(prompt, str) or not 1 <= len(prompt.encode()) <= _MAX_WAVE_PROMPT_BYTES
                or plan.get("initial_head") != manifest["initial_head"] or plan.get("prompt_fresh") is not True
                or not isinstance(plan.get("prompt_nonce"), str) or not 1 <= len(plan["prompt_nonce"].encode()) <= 256
                or not _digest(plan.get("prompt_sha256"))
                or not hmac.compare_digest(plan["prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
                or _safe_relative(plan.get("files_modified")) is None
                or _safe_relative(plan.get("files_deleted")) is None
                ):
            raise WorkerChannelRefused("IPC_WAVE_MANIFEST_INVALID")
        dependencies = plan.get("depends_on", [])
        if (not isinstance(dependencies, list) or len(dependencies) > _MAX_WAVE_PLANS
                or any(not isinstance(item, str) or not item or item == plan_id for item in dependencies)):
            raise WorkerChannelRefused("IPC_WAVE_MANIFEST_INVALID")
        plan_ids.add(plan_id)
    # A runnable wave may retain dependencies on earlier waves. The owner-side
    # consumer proves these against durable successful replies before effects.
    return manifest, canonical


def _parent_pid(identity: ProcessIdentity) -> int | None:
    """Read one parent PID only while the child incarnation remains live."""
    if probe_identity(identity) != LIVE:
        return None
    try:
        if sys.platform.startswith("linux"):
            stat_line = Path(f"/proc/{identity.pid}/stat").read_text(encoding="ascii")
            parent = int(stat_line.rsplit(")", 1)[1].strip().split()[1])
        elif sys.platform == "darwin":
            result = subprocess.run(
                ["/bin/ps", "-o", "ppid=", "-p", str(identity.pid)], capture_output=True,
                text=True, timeout=1, check=True, env={"PATH": "/usr/bin:/bin"},
            )
            parent = int(result.stdout.strip())
        else:
            return None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return parent if parent > 0 and probe_identity(identity) == LIVE else None


def _live_descendant(peer: ProcessIdentity, ancestor: ProcessIdentity) -> bool:
    """Prove every native ancestor edge, including each PID incarnation."""
    if probe_identity(peer) != LIVE or probe_identity(ancestor) != LIVE:
        return False
    current, seen = peer, set()
    for _depth in range(64):
        if current == ancestor:
            return probe_identity(ancestor) == LIVE
        if current.pid in seen:
            return False
        seen.add(current.pid)
        parent_pid = _parent_pid(current)
        if parent_pid is None:
            return False
        try:
            parent = ProcessIdentity.from_pid(parent_pid)
        except (ProcessLookupError, ValueError):
            return False
        if probe_direct_parent(current, parent) != LIVE:
            return False
        current = parent
    return False


def _send(channel, value, *, max_bytes=65536):
    raw = _canonical(value)
    if len(raw) > max_bytes:
        raise WorkerChannelRefused("IPC_MESSAGE_TOO_LARGE")
    channel.sendall(len(raw).to_bytes(4, "big") + raw)


def _receive(channel, *, max_bytes=65536):
    def exact(size):
        parts = bytearray()
        while len(parts) < size:
            part = channel.recv(size - len(parts))
            if not part:
                raise WorkerChannelRefused("IPC_CLOSED")
            parts.extend(part)
        return bytes(parts)

    size = int.from_bytes(exact(4), "big")
    if not 0 < size <= max_bytes:
        raise WorkerChannelRefused("IPC_MESSAGE_TOO_LARGE")
    try:
        message = json.loads(exact(size))
        _canonical(message)
    except (ValueError, UnicodeError, TypeError) as error:
        raise WorkerChannelRefused("IPC_INVALID_MESSAGE") from error
    if not isinstance(message, dict):
        raise WorkerChannelRefused("IPC_INVALID_MESSAGE")
    return message


def peer_identity(channel: socket.socket) -> ProcessIdentity:
    """Read kernel peer credentials and capture the peer's full native identity."""
    try:
        if sys.platform == "linux":
            pid, uid, _gid = struct.unpack(
                "3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
            )
        elif sys.platform == "darwin":
            # Apple xnu bsd/sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=0x002.
            pid = struct.unpack("i", channel.getsockopt(0, 0x002, 4))[0]
            uid_value, gid_value = ctypes.c_uint(), ctypes.c_uint()
            native = ctypes.CDLL(None, use_errno=True)
            getpeereid = native.getpeereid
            getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
            getpeereid.restype = ctypes.c_int
            if getpeereid(channel.fileno(), ctypes.byref(uid_value), ctypes.byref(gid_value)) != 0:
                raise OSError(ctypes.get_errno(), "getpeereid failed")
            uid = uid_value.value
        else:
            raise WorkerChannelRefused("IPC_PEER_IDENTITY_UNAVAILABLE")
        if uid != os.getuid() or pid <= 0:
            raise WorkerChannelRefused("IPC_PEER_MISMATCH")
        identity = ProcessIdentity.from_pid(pid)
        if probe_identity(identity) != LIVE:
            raise WorkerChannelRefused("IPC_PEER_UNKNOWN")
        return identity
    except (OSError, ValueError, struct.error) as error:
        raise WorkerChannelRefused("IPC_PEER_IDENTITY_UNAVAILABLE") from error


@dataclass(frozen=True)
class WorkerBinding:
    repository_id: str
    run_id: str
    activity_id: str
    intent_id: str
    generation: int
    identity: ProcessIdentity
    workspace: str
    runtime_identity: str
    candidate_hash: str
    contract_hash: str
    allowed_roles: tuple[str, ...]
    allowed_workspaces: tuple[str, ...]
    supervisor_identity: ProcessIdentity

    def scope(self) -> dict:
        """Public routing metadata; never includes an owner nonce or DB path."""
        return {**{key: getattr(self, key) for key in (
            "repository_id", "run_id", "activity_id", "intent_id", "generation",
        )}, "supervisor_identity": asdict(self.supervisor_identity)}


@dataclass(frozen=True)
class _BrokerBootstrap:
    """An in-memory, single-use handoff from Supervisor to one child process."""

    binding: WorkerBinding
    broker_identity: ProcessIdentity


def _bootstrap_token_digest(value) -> bytes | None:
    """Return a verifier without retaining the bearer secret itself."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        return None
    return hashlib.sha256(value.encode("ascii")).digest()


class WorkerChannelServer:
    """Local socket owned by the supervisor, serving only registered live peers."""

    def __init__(self, store, token, endpoint: str | Path):
        self.store, self.token = store, token
        self.endpoint = Path(endpoint)
        parent = self.endpoint.parent
        if (not self.endpoint.is_absolute() or parent.resolve() != parent
                or not parent.is_dir() or parent.is_symlink()
                or parent.stat().st_uid != os.getuid()
                or stat.S_IMODE(parent.stat().st_mode) != 0o700):
            raise WorkerChannelRefused("UNSAFE_IPC_ENDPOINT")
        self._parent_identity = (parent.stat().st_dev, parent.stat().st_ino)
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._socket.bind(str(self.endpoint))
            os.chmod(self.endpoint, 0o600)
            self._socket_identity = (self.endpoint.lstat().st_dev, self.endpoint.lstat().st_ino)
            self._socket.listen(8)
            self._socket.settimeout(0.2)
        except BaseException:
            self._socket.close()
            raise
        self._bindings: dict[int, WorkerBinding] = {}
        # Primary entries stay distinct from brokers so a broker can never be
        # used as the parent of another broker.
        self._primary_bindings: dict[str, WorkerBinding] = {}
        self._broker_bootstraps: dict[bytes, _BrokerBootstrap] = {}
        self._broker_bootstrap_intents: dict[str, bytes] = {}
        self._broker_identities: dict[str, ProcessIdentity] = {}
        # Codex's qualified macOS sandbox denies AF_UNIX even for a private
        # socket.  Production host children therefore receive a bounded,
        # capability-authenticated request directory inside their registered
        # workspace.  The Unix socket remains authoritative for native
        # workers and brokers; both transports enter the same fenced handler.
        self._file_bindings: dict[Path, tuple[bytes, WorkerBinding, tuple[int, int]]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        # The channel only authenticates and journals a delegation request.
        # A Supervisor may attach the owner-side allocator; it is deliberately
        # called only after the request transaction and channel lock are gone.
        self._delegate_consumer = None
        self._wave_consumer = None

    def attach_delegate_consumer(self, consumer):
        if not callable(consumer):
            raise WorkerChannelRefused("IPC_INVALID_REGISTRATION")
        with self._lock:
            if self._delegate_consumer not in (None, consumer):
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            self._delegate_consumer = consumer

    def attach_wave_consumer(self, consumer):
        """Attach the owner-only consumer for one retained GSD wave request."""
        if not callable(consumer):
            raise WorkerChannelRefused("IPC_INVALID_REGISTRATION")
        with self._lock:
            if self._wave_consumer not in (None, consumer):
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            self._wave_consumer = consumer

    def _verify_binding(self, tx, binding):
        assert_owner(tx, self.token)
        row = tx.execute(
            "SELECT i.*,a.repository_id,a.run_id,a.runtime_tuple_hash,a.state AS activity_state "
            "FROM authority_launch_intents i "
            "JOIN authority_activities a ON a.id=i.activity_id WHERE i.id=?", (binding.intent_id,),
        ).fetchone()
        if (row is None or row["repository_id"] != binding.repository_id
                or row["run_id"] != binding.run_id or row["activity_id"] != binding.activity_id
                or row["generation"] != self.token.generation
                or row["state"] != "released_to_execute"
                or row["activity_state"] in {"succeeded", "failed", "aborted"}
                or row["runtime_tuple_hash"] != binding.runtime_identity
                or (row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"])
                != (binding.identity.host_id, binding.identity.boot_id, binding.identity.pid, binding.identity.start_token)):
            raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
        child = tx.execute(
            "SELECT * FROM authority_child_bindings WHERE activity_id=?", (binding.activity_id,),
        ).fetchone()
        if (child is None or child["workspace_binding"] != binding.workspace
                or child["candidate_hash"] != binding.candidate_hash
                or child["contract_hash"] != binding.contract_hash
                or child["runtime_identity"] != binding.runtime_identity):
            raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
        preparation = tx.execute(
            "SELECT * FROM context_workspaces WHERE preparation_id=? AND state='ready' AND created_by_ffs=1",
            (child["workspace_preparation_id"],),
        ).fetchone()
        if (preparation is None or preparation["path"] != binding.workspace
                or preparation["parent_activity_id"] != child["parent_activity_id"]
                or preparation["child_role"] != child["role"]):
            raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
        from run_state.workspace import _assert_preparation_binding
        _assert_preparation_binding(preparation, self.token, require_generation=True, tx=tx)

    def register_worker(self, intent_id: str, *, contract_hash: str,
                        allowed_roles=("worker", "reviewer"), allowed_workspaces=()) -> dict:
        """Register an already authorized child; the caller is the trusted supervisor."""
        if (not _digest(contract_hash) or not allowed_roles
                or any(role not in {"worker", "reviewer", "recovery", "inventory"} for role in allowed_roles)):
            raise WorkerChannelRefused("IPC_INVALID_REGISTRATION")
        supervisor_identity = ProcessIdentity.current()
        with self.store.transaction() as tx:
            assert_owner(tx, self.token)
            row = tx.execute(
                "SELECT i.*,a.repository_id,a.run_id,a.runtime_tuple_hash,a.input_digest "
                "FROM authority_launch_intents i JOIN authority_activities a ON a.id=i.activity_id "
                "WHERE i.id=?", (intent_id,),
            ).fetchone()
            if (row is None or row["repository_id"] != self.token.repository_id
                    or row["run_id"] != self.token.run_id or row["generation"] != self.token.generation
                    or row["state"] != "released_to_execute" or row["child_pid"] is None):
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            child = tx.execute(
                "SELECT * FROM authority_child_bindings WHERE activity_id=?", (row["activity_id"],),
            ).fetchone()
            if (child is None or child["contract_hash"] != contract_hash
                    or child["runtime_identity"] != row["runtime_tuple_hash"]):
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            preparation = tx.execute(
                "SELECT * FROM context_workspaces WHERE preparation_id=? AND repository_id=? AND run_id=? "
                "AND generation=? AND state='ready' AND created_by_ffs=1",
                (child["workspace_preparation_id"], self.token.repository_id, self.token.run_id,
                 self.token.generation),
            ).fetchone()
            if (preparation is None or preparation["path"] != child["workspace_binding"]
                    or preparation["parent_activity_id"] != child["parent_activity_id"]
                    or preparation["child_role"] != child["role"]):
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            from run_state.workspace import _assert_preparation_binding
            try:
                _assert_preparation_binding(preparation, self.token, require_generation=True, tx=tx)
            except OwnershipRefused as error:
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH") from error
            workspace = child["workspace_binding"]
            candidate = child["candidate_hash"]
            if not _digest(candidate) or not row["runtime_tuple_hash"]:
                raise WorkerChannelRefused("IPC_INVALID_REGISTRATION")
            workspaces = tuple(dict.fromkeys((workspace, *allowed_workspaces)))
            for path in workspaces:
                if not isinstance(path, str) or not Path(path).is_absolute() or Path(path).resolve() != Path(path):
                    raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
                if tx.execute(
                    "SELECT 1 FROM context_workspaces WHERE repository_id=? AND run_id=? "
                    "AND path=? AND generation=? AND state='ready'",
                    (self.token.repository_id, self.token.run_id, path, self.token.generation),
                ).fetchone() is None:
                    raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            identity = ProcessIdentity(row["child_host_id"], row["child_boot_id"], row["child_pid"], row["child_start_token"])
            binding = WorkerBinding(
                self.token.repository_id, self.token.run_id, row["activity_id"], intent_id,
                self.token.generation, identity, workspace, row["runtime_tuple_hash"],
                candidate, contract_hash, tuple(allowed_roles), workspaces,
                supervisor_identity,
            )
        if probe_identity(identity) != LIVE:
            raise WorkerChannelRefused("IPC_PEER_UNKNOWN")
        self.store.record_event_once(
            self.token, binding.activity_id, "worker-registration:" + intent_id,
            asdict(binding),
        )
        with self._lock:
            previous = self._bindings.get(identity.pid)
            if previous is not None and previous != binding:
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            primary = self._primary_bindings.get(intent_id)
            if primary is not None and primary != binding:
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            self._bindings[identity.pid] = binding
            self._primary_bindings[intent_id] = binding
        return binding.scope()

    def register_file_transport(self, intent_id: str) -> dict[str, str]:
        """Publish one private workspace channel for an already registered child."""
        with self.store.fenced_operation(self.token):
            with self._lock:
                binding = self._primary_bindings.get(intent_id)
                if binding is None:
                    raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
                with self.store.transaction() as tx:
                    self._verify_binding(tx, binding)
                workspace = Path(binding.workspace)
                planning = workspace / ".planning"
                try:
                    planning.mkdir(mode=0o700, exist_ok=True)
                    planning_info = planning.lstat()
                    if (planning.is_symlink() or not planning.is_dir()
                            or planning_info.st_uid != os.getuid()
                            or planning_info.st_mode & 0o022):
                        raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
                    parent = planning / _FILE_CHANNEL_DIRECTORY
                    parent.mkdir(mode=0o700, exist_ok=True)
                    os.chmod(parent, 0o700)
                    root = parent / hashlib.sha256(intent_id.encode()).hexdigest()[:24]
                    root.mkdir(mode=0o700)
                    requests = root / "requests"
                    responses = root / "responses"
                    requests.mkdir(mode=0o700)
                    responses.mkdir(mode=0o700)
                    info = root.lstat()
                    if (root.is_symlink() or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o700):
                        raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
                except (FileExistsError, OSError) as error:
                    raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE") from error
                token = secrets.token_urlsafe(32)
                digest = _bootstrap_token_digest(token)
                assert digest is not None
                self._file_bindings[root] = (digest, binding, (info.st_dev, info.st_ino))
                return {"root": str(root), "capability": token}

    @staticmethod
    def _file_payload(path: Path, *, maximum: int) -> dict:
        try:
            info = path.lstat()
            if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1 or not 0 < info.st_size <= maximum):
                raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
            raw = path.read_bytes()
            if len(raw) != info.st_size:
                raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
            value = json.loads(raw, object_pairs_hook=_no_duplicate_object)
            if not isinstance(value, dict) or _canonical(value) != raw:
                raise WorkerChannelRefused("IPC_INVALID_MESSAGE")
            return value
        except WorkerChannelRefused:
            raise
        except (OSError, ValueError, UnicodeError, TypeError) as error:
            raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE") from error

    @staticmethod
    def _publish_file_response(directory: Path, name: str, value: dict) -> None:
        raw = _canonical(value)
        if len(raw) > _MAX_WAVE_REPLY_BYTES:
            raise WorkerChannelRefused("IPC_MESSAGE_TOO_LARGE")
        info = directory.lstat()
        if (directory.is_symlink() or not directory.is_dir() or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
        temporary = directory / ("." + name + "." + secrets.token_hex(8))
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, directory / name, follow_symlinks=False)
        except FileExistsError as error:
            raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE") from error
        finally:
            temporary.unlink(missing_ok=True)

    def _serve_file_once(self) -> bool:
        with self._lock:
            bindings = tuple(self._file_bindings.items())
        for root, (expected_token, binding, identity) in bindings:
            try:
                info = root.lstat()
                if (root.is_symlink() or (info.st_dev, info.st_ino) != identity
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
                candidates = sorted((root / "requests").glob("*.json"))
            except OSError:
                continue
            for path in candidates:
                response = None
                try:
                    wrapped = self._file_payload(path, maximum=65536)
                    token_digest = _bootstrap_token_digest(wrapped.get("capability"))
                    if (set(wrapped) != {"capability", "message"} or token_digest is None
                            or not hmac.compare_digest(token_digest, expected_token)):
                        raise WorkerChannelRefused("IPC_FILE_CAPABILITY_REFUSED")
                    response = self._request(binding.identity, wrapped["message"])
                except (WorkerChannelRefused, OwnershipRefused, ControlStoreRefused) as error:
                    response = {"ok": False, "code": error.code}
                except Exception as error:
                    from .supervisor import SupervisorRefused
                    from .workspace import WorkspaceRefused
                    if isinstance(error, (SupervisorRefused, WorkspaceRefused)):
                        response = {"ok": False, "code": error.code}
                    elif isinstance(error, (OSError, ValueError, TypeError, KeyError)):
                        response = {"ok": False, "code": "IPC_INVALID_MESSAGE"}
                    else:
                        raise
                self._publish_file_response(root / "responses", path.name, response)
                path.unlink()
                return True
        return False

    def issue_broker_bootstrap(self, intent_id: str, broker_identity: ProcessIdentity) -> str:
        """Approve one live direct child of a registered worker as its broker.

        This is a Supervisor-only handoff point. The returned bearer token is
        generated here, retained only as a digest in memory, and invalidated on
        successful registration or when this server closes. The caller delivers
        it to the already launched child; this channel has no launch interface.
        """
        if not isinstance(broker_identity, ProcessIdentity) or probe_identity(broker_identity) != LIVE:
            raise WorkerChannelRefused("IPC_BROKER_IDENTITY_MISMATCH")
        # Use the same owner-fence -> channel-lock order as ordinary requests.
        # Reversing those two locks can make concurrent registration time out
        # against a request that is already inside the owner fence.
        with self.store.fenced_operation(self.token):
            with self._lock:
                binding = self._primary_bindings.get(intent_id)
                if binding is None or intent_id in self._broker_identities:
                    raise WorkerChannelRefused("IPC_BROKER_NOT_AVAILABLE")
                if intent_id in self._broker_bootstrap_intents:
                    raise WorkerChannelRefused("IPC_BROKER_ALREADY_APPROVED")
                if (probe_identity(binding.identity) != LIVE
                        or probe_direct_parent(broker_identity, binding.identity) != LIVE):
                    raise WorkerChannelRefused("IPC_BROKER_ANCESTRY_MISMATCH")
                with self.store.transaction() as tx:
                    self._verify_binding(tx, binding)
                # token_urlsafe(32) yields 256 bits and the fixed encoding below.
                token = secrets.token_urlsafe(32)
                token_digest = _bootstrap_token_digest(token)
                assert token_digest is not None
                self._broker_bootstraps[token_digest] = _BrokerBootstrap(binding, broker_identity)
                self._broker_bootstrap_intents[intent_id] = token_digest
                return token

    def _binding_for_peer_locked(self, peer: ProcessIdentity) -> WorkerBinding:
        binding = self._bindings.get(peer.pid)
        if binding is None:
            raise WorkerChannelRefused("IPC_PEER_MISMATCH")
        if binding.identity == peer:
            if probe_identity(binding.identity) != LIVE:
                raise WorkerChannelRefused("IPC_PEER_UNKNOWN")
            return binding
        broker_identity = self._broker_identities.get(binding.intent_id)
        if broker_identity != peer:
            raise WorkerChannelRefused("IPC_PEER_MISMATCH")
        if (probe_identity(binding.identity) != LIVE
                or probe_direct_parent(peer, binding.identity) != LIVE):
            raise WorkerChannelRefused("IPC_BROKER_ANCESTRY_MISMATCH")
        return binding

    def _wave_binding_for_peer_locked(self, peer: ProcessIdentity, message: dict) -> WorkerBinding:
        """Route GSD's descendant request only after native ancestry proof."""
        intent_id = message.get("intent_id")
        binding = self._primary_bindings.get(intent_id) if isinstance(intent_id, str) else None
        if binding is None:
            raise WorkerChannelRefused("IPC_PEER_MISMATCH")
        if peer == binding.identity:
            if probe_identity(binding.identity) != LIVE:
                raise WorkerChannelRefused("IPC_PEER_UNKNOWN")
            return binding
        if not _live_descendant(peer, binding.identity):
            raise WorkerChannelRefused("IPC_DESCENDANT_ANCESTRY_MISMATCH")
        return binding

    def assert_authorized_peer(self, intent_id: str, peer: ProcessIdentity) -> None:
        """Revalidate an exact primary or approved broker for owner-side replay."""
        with self._lock:
            binding = self._binding_for_peer_locked(peer)
            if binding.intent_id != intent_id:
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")

    def assert_authorized_wave_peer(self, intent_id: str, peer: ProcessIdentity) -> None:
        """Revalidate GSD's exact primary or a live, full-ancestry descendant."""
        with self._lock:
            binding = self._primary_bindings.get(intent_id)
            if binding is None:
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            if peer == binding.identity:
                if probe_identity(peer) != LIVE:
                    raise WorkerChannelRefused("IPC_PEER_UNKNOWN")
                return
            if not _live_descendant(peer, binding.identity):
                raise WorkerChannelRefused("IPC_DESCENDANT_ANCESTRY_MISMATCH")

    def _find_bootstrap(self, token) -> tuple[bytes, _BrokerBootstrap] | None:
        token_digest = _bootstrap_token_digest(token)
        if token_digest is None:
            return None
        # Do not let dictionary early-exit comparison decide a bearer-token
        # authentication result.
        for known_digest, bootstrap in self._broker_bootstraps.items():
            if hmac.compare_digest(token_digest, known_digest):
                return known_digest, bootstrap
        return None

    def _register_broker_fenced(self, peer: ProcessIdentity, message: dict) -> dict:
        """Consume a Supervisor-issued bootstrap from its exact child process."""
        with self._lock:
            found = self._find_bootstrap(message.get("bootstrap_token"))
            if found is None:
                raise WorkerChannelRefused("IPC_BROKER_BOOTSTRAP_REFUSED")
            token_digest, bootstrap = found
            binding = bootstrap.binding
            expected = {"schema_version", "bootstrap_token", *binding.scope()}
            if (set(message) != expected or type(message["schema_version"]) is not int
                    or message["schema_version"] != 1
                    or {key: message[key] for key in binding.scope()} != binding.scope()
                    or peer != bootstrap.broker_identity
                    or probe_identity(peer) != LIVE
                    or probe_identity(binding.identity) != LIVE
                    or probe_direct_parent(peer, binding.identity) != LIVE):
                raise WorkerChannelRefused("IPC_BROKER_BOOTSTRAP_REFUSED")
            if (self._primary_bindings.get(binding.intent_id) != binding
                    or binding.intent_id in self._broker_identities):
                raise WorkerChannelRefused("IPC_BROKER_BOOTSTRAP_REFUSED")
            previous = self._bindings.get(peer.pid)
            if previous is not None and previous != binding:
                raise WorkerChannelRefused("IPC_BROKER_BOOTSTRAP_REFUSED")
            with self.store.transaction() as tx:
                self._verify_binding(tx, binding)
            self._bindings[peer.pid] = binding
            self._broker_identities[binding.intent_id] = peer
            del self._broker_bootstraps[token_digest]
            del self._broker_bootstrap_intents[binding.intent_id]
            return {"ok": True, "scope": binding.scope()}

    def _request(self, peer: ProcessIdentity, message: dict) -> dict:
        if probe_identity(peer) != LIVE:
            raise WorkerChannelRefused("IPC_PEER_UNKNOWN")
        # Revalidation and request-event commit are ordered
        # together against revocation without holding one SQLite transaction
        # across the existing idempotent store operations.
        with self.store.fenced_operation(self.token):
            response = (self._register_broker_fenced(peer, message)
                        if "bootstrap_token" in message else self._request_fenced(peer, message))
        # Do not run Git or create authority records while the request lock or
        # its fenced section is held.  The consumer rereads the durable event.
        if response.get("allocation_event_id") is not None:
            with self._lock:
                consumer = self._delegate_consumer
            if consumer is not None:
                allocation = consumer(response.pop("allocation_event_id"))
                response["result"] = allocation
        if response.get("wave_event_id") is not None:
            with self._lock:
                consumer = self._wave_consumer
            if consumer is not None:
                wave_result = consumer(response.pop("wave_event_id"))
                if not isinstance(wave_result, dict):
                    raise WorkerChannelRefused("IPC_MESSAGE_TOO_LARGE")
                response["result"] = wave_result
                if len(_canonical(response)) > _MAX_WAVE_REPLY_BYTES:
                    raise WorkerChannelRefused("IPC_MESSAGE_TOO_LARGE")
        return response

    def _request_fenced(self, peer: ProcessIdentity, message: dict) -> dict:
        with self._lock:
            binding = (self._wave_binding_for_peer_locked(peer, message)
                       if message.get("operation") == "gsd-wave-request"
                       else self._binding_for_peer_locked(peer))
            expected = {"schema_version", "request_key", "operation", "body", *binding.scope()}
            if (set(message) != expected or type(message["schema_version"]) is not int
                    or message["schema_version"] != 1
                    or {key: message[key] for key in binding.scope()} != binding.scope()
                    or type(message["generation"]) is not int):
                raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            key, operation, body = message["request_key"], message["operation"], message["body"]
            if (not isinstance(key, str) or not 1 <= len(key.encode()) <= 128
                    or not isinstance(operation, str) or not isinstance(body, dict)):
                raise WorkerChannelRefused("IPC_INVALID_MESSAGE")
            with self.store.transaction() as tx:
                self._verify_binding(tx, binding)
            result = {"status": "requested"}
            if operation == "progress":
                if (set(body) != {"sequence", "message"} or type(body["sequence"]) is not int
                        or not 0 <= body["sequence"] <= 9_223_372_036_854_775_807
                        or not isinstance(body["message"], str) or len(body["message"].encode()) > 4096):
                    raise WorkerChannelRefused("IPC_INVALID_MESSAGE")
                result = {"status": "recorded"}
            elif operation == "evidence-request":
                if set(body) != {"path", "sha256"} or not isinstance(body["path"], str) or not _digest(body["sha256"]):
                    raise WorkerChannelRefused("IPC_INVALID_MESSAGE")
                relative = PurePosixPath(body["path"])
                if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                        or "\\" in body["path"] or "\0" in body["path"]):
                    raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
            elif operation == "delegate-request":
                fields = {"parent_activity_id", "role", "candidate_hash", "contract_hash", "runtime_identity"}
                if (set(body) != fields or body["parent_activity_id"] != binding.activity_id
                        or body["role"] not in binding.allowed_roles
                        or body["candidate_hash"] != binding.candidate_hash
                        or body["contract_hash"] != binding.contract_hash
                        or body["runtime_identity"] != binding.runtime_identity):
                    raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
                result = {"status": "pending_allocation"}
            elif operation == "gsd-wave-request":
                if set(body) != {"manifest_locator", "manifest_sha256"}:
                    raise WorkerChannelRefused("IPC_INVALID_MESSAGE")
                locator, manifest_hash = body["manifest_locator"], body["manifest_sha256"]
                if (not isinstance(locator, str) or not _digest(manifest_hash)
                        or len(locator.encode()) > 512):
                    raise WorkerChannelRefused("IPC_INVALID_MESSAGE")
                relative = PurePosixPath(locator)
                if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                        or any(part in {"", "."} for part in relative.parts)
                        or "\\" in locator or "\0" in locator):
                    raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
                manifest = self._read_wave_manifest(binding, relative, manifest_hash)
                admission = manifest["admission"]
                if admission != {
                    "schema": "ffs.supervisor-admission/v1", "available": True,
                    "repository_id": binding.repository_id, "run_id": binding.run_id,
                    "activity_id": binding.activity_id, "generation": binding.generation,
                    "workspace": binding.workspace, "runtime_identity": binding.runtime_identity,
                }:
                    raise WorkerChannelRefused("IPC_SCOPE_MISMATCH")
                result = {"status": "pending_wave"}
            else:
                raise WorkerChannelRefused("IPC_OPERATION_FORBIDDEN")
            event_key = "worker-request:" + binding.intent_id + ":" + key
            payload = {"operation": operation, "body": body, "intent_id": binding.intent_id,
                       "peer_identity": asdict(peer), "workspace": binding.workspace,
                       "runtime_identity": binding.runtime_identity}
            if operation == "gsd-wave-request":
                payload["manifest"] = manifest
                if len(_canonical(payload)) > 32768:
                    raise WorkerChannelRefused("IPC_MESSAGE_TOO_LARGE")
            # Commit a pending allocator request only. A worker never selects a
            # workspace or creates an activity; the owner allocates both later.
            with self.store.transaction() as tx:
                prior = tx.execute(
                    "SELECT 1 FROM authority_event_keys WHERE activity_id=? AND idempotency_key=?",
                    (binding.activity_id, event_key),
                ).fetchone()
            event = self.store.record_event_once(self.token, binding.activity_id, event_key, payload)
            response = {"ok": True, "request_key": key, "replayed": prior is not None,
                    "event_id": event["id"], "body_sha256": hashlib.sha256(_canonical(body)).hexdigest(),
                    "result": result}
            if operation == "delegate-request":
                response["allocation_event_id"] = event["id"]
            if operation == "gsd-wave-request":
                response["wave_event_id"] = event["id"]
            return response

    @staticmethod
    def _read_wave_manifest(binding: WorkerBinding, relative: PurePosixPath, expected_hash: str) -> dict:
        """Read a regular manifest beneath the registered workspace without path traversal."""
        root = Path(binding.workspace)
        try:
            root_info = root.lstat()
            if (root.is_symlink() or not root.is_dir() or root.resolve() != root
                    or root_info.st_uid != os.getuid()):
                raise WorkerChannelRefused("IPC_WAVE_LOCATOR_INVALID")
            opened_directory = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            opened_root = os.fstat(opened_directory)
            if (opened_root.st_dev, opened_root.st_ino) != (root_info.st_dev, root_info.st_ino):
                os.close(opened_directory)
                raise WorkerChannelRefused("IPC_WAVE_LOCATOR_INVALID")
            directory = opened_directory
        except (OSError, ValueError) as error:
            raise WorkerChannelRefused("IPC_WAVE_LOCATOR_INVALID") from error
        try:
            for component in relative.parts[:-1]:
                next_directory = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = next_directory
            descriptor = os.open(
                relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > _MAX_WAVE_MANIFEST_BYTES:
                    raise WorkerChannelRefused("IPC_WAVE_LOCATOR_INVALID")
                content = bytearray()
                while len(content) <= _MAX_WAVE_MANIFEST_BYTES:
                    chunk = os.read(descriptor, _MAX_WAVE_MANIFEST_BYTES + 1 - len(content))
                    if not chunk:
                        break
                    content.extend(chunk)
                raw = bytes(content)
            finally:
                os.close(descriptor)
        except WorkerChannelRefused:
            raise
        except OSError as error:
            raise WorkerChannelRefused("IPC_WAVE_LOCATOR_INVALID") from error
        finally:
            os.close(directory)
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_hash):
            raise WorkerChannelRefused("IPC_WAVE_MANIFEST_HASH_MISMATCH")
        manifest, _canonical_manifest = parse_gsd_wave_manifest(raw)
        try:
            orchestrator = Path(manifest["orchestrator_root"])
            orchestrator_info = orchestrator.lstat()
            if (orchestrator_info.st_uid != os.getuid() or orchestrator.is_symlink()
                    or not orchestrator.is_dir() or orchestrator.resolve() != root):
                raise WorkerChannelRefused("IPC_WAVE_LOCATOR_INVALID")
            if (orchestrator_info.st_dev, orchestrator_info.st_ino) != (root_info.st_dev, root_info.st_ino):
                raise WorkerChannelRefused("IPC_WAVE_LOCATOR_INVALID")
        except OSError as error:
            raise WorkerChannelRefused("IPC_WAVE_LOCATOR_INVALID") from error
        return manifest

    def serve_once(self) -> bool:
        try:
            channel, _ = self._socket.accept()
        except socket.timeout:
            return False
        with channel:
            channel.settimeout(3)
            response_limit = 65536
            try:
                peer = peer_identity(channel)
                message = _receive(channel)
                if message.get("operation") == "gsd-wave-request":
                    response_limit = _MAX_WAVE_REPLY_BYTES
                response = self._request(peer, message)
            except (WorkerChannelRefused, OwnershipRefused, ControlStoreRefused) as error:
                response = {"ok": False, "code": error.code}
            except Exception as error:
                # Allocation refusals are expected protocol results.  Keep
                # malformed wire data and OS faults contained as well, while
                # leaving unrelated programming failures observable.
                from .supervisor import SupervisorRefused
                from .workspace import WorkspaceRefused
                if isinstance(error, (SupervisorRefused, WorkspaceRefused)):
                    response = {"ok": False, "code": error.code}
                elif isinstance(error, (OSError, ValueError, TypeError, KeyError)):
                    response = {"ok": False, "code": "IPC_INVALID_MESSAGE"}
                else:
                    raise
            try:
                _send(channel, response, max_bytes=response_limit)
            except OSError:
                pass
        return True

    def start(self):
        if self._thread is not None:
            raise WorkerChannelRefused("IPC_ALREADY_STARTED")

        def serve():
            while not self._stop.is_set():
                try:
                    socket_work = self.serve_once()
                    file_work = self._serve_file_once()
                    if not socket_work and not file_work:
                        self._stop.wait(0.02)
                except OSError:
                    if not self._stop.is_set():
                        self._stop.set()

        self._thread = threading.Thread(target=serve, name="ffs-worker-requests", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=4)
        self._socket.close()
        with self._lock:
            self._broker_bootstraps.clear()
            self._broker_bootstrap_intents.clear()
            self._file_bindings.clear()
        parent = self.endpoint.parent
        try:
            if ((parent.stat().st_dev, parent.stat().st_ino) == self._parent_identity
                    and (self.endpoint.lstat().st_dev, self.endpoint.lstat().st_ino) == self._socket_identity):
                self.endpoint.unlink()
        except FileNotFoundError:
            pass


def _request_from_supervisor(endpoint: str | Path, scope: dict, message: dict, timeout: float) -> dict:
    """Send one request after authenticating the local Supervisor socket peer."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(timeout)
        channel.connect(str(endpoint))
        try:
            supervisor = ProcessIdentity(**scope["supervisor_identity"])
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerChannelRefused("IPC_SUPERVISOR_MISMATCH") from error
        if peer_identity(channel) != supervisor:
            raise WorkerChannelRefused("IPC_SUPERVISOR_MISMATCH")
        _send(channel, message)
        return _receive(channel, max_bytes=(
            _MAX_WAVE_REPLY_BYTES if message.get("operation") == "gsd-wave-request" else 65536
        ))


def broker_register(endpoint: str | Path, scope: dict, *, bootstrap_token: str,
                    timeout: float = 5) -> dict:
    """Consume a Supervisor-provided broker bootstrap and return its routing scope."""
    response = _request_from_supervisor(
        endpoint, scope, {"schema_version": 1, **scope, "bootstrap_token": bootstrap_token}, timeout,
    )
    if response.get("ok") is not True or response.get("scope") != scope:
        raise WorkerChannelRefused(response.get("code", "IPC_BROKER_BOOTSTRAP_REFUSED"))
    return scope


def request(endpoint: str | Path, scope: dict, *, request_key: str, operation: str, body: dict,
            timeout: float = 5) -> dict:
    """Worker-side client. The OS socket peer identity supplies authentication."""
    message = {"schema_version": 1, **scope, "request_key": request_key,
               "operation": operation, "body": body}
    return _request_from_supervisor(endpoint, scope, message, timeout)


def file_request(root: str | Path, capability: str, scope: dict, *, request_key: str,
                 operation: str, body: dict, timeout: float | None = 5) -> dict:
    """Use the workspace file transport when a host sandbox denies AF_UNIX."""
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root:
        raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
    try:
        info = root.lstat()
        if (root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
        for name in ("requests", "responses"):
            child = root / name
            child_info = child.lstat()
            if (child.is_symlink() or not child.is_dir() or child_info.st_uid != os.getuid()
                    or stat.S_IMODE(child_info.st_mode) != 0o700):
                raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE")
    except OSError as error:
        raise WorkerChannelRefused("IPC_FILE_CHANNEL_UNSAFE") from error
    message = {"schema_version": 1, **scope, "request_key": request_key,
               "operation": operation, "body": body}
    wrapped = _canonical({"capability": capability, "message": message})
    if len(wrapped) > 65536:
        raise WorkerChannelRefused("IPC_MESSAGE_TOO_LARGE")
    name = secrets.token_hex(16) + ".json"
    destination = root / "requests" / name
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.write(descriptor, wrapped)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    response = root / "responses" / name
    deadline = None if timeout is None else time.monotonic() + timeout
    while not response.exists():
        if deadline is not None and time.monotonic() >= deadline:
            raise WorkerChannelRefused("IPC_TIMEOUT")
        time.sleep(0.02)
    try:
        return WorkerChannelServer._file_payload(response, maximum=_MAX_WAVE_REPLY_BYTES)
    finally:
        try:
            response.unlink()
        except FileNotFoundError:
            pass
