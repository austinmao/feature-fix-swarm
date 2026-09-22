"""Closed, supervisor-owned containment policy for one worker process.

This module deliberately describes an execution boundary; it does not execute
workers or mint authority.  A host adapter must use ``build_contained_argv``
and may not replace an unavailable platform mechanism with an ordinary child.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


class WorkerPolicyRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WorkerRegistration:
    repository_id: str
    run_id: str
    activity_id: str
    attempt_id: str
    generation: int
    workspace: str
    primary_root: str
    state_root: str
    git_common_dir: str
    sibling_roots: tuple[str, ...]
    socket_root: str
    ipc_endpoint: str
    policy_root: str
    fixture_epoch_id: str
    capability_receipt_id: str
    composition_evidence_hash: str


@dataclass(frozen=True)
class WorkerRuntimeRoots:
    read_only_roots: tuple[str, ...]
    attempt_scratch: str
    runtime_tuple_hash: str
    manifest_sha256: str


@dataclass(frozen=True)
class ArtifactReviewRoots:
    """Supervisor-selected public artifacts, runtime reads, and one scratch dir."""
    artifact_root: str
    runtime_read_only_roots: tuple[str, ...]
    attempt_scratch: str
    runtime_tuple_hash: str
    manifest_sha256: str
    executable: str


@dataclass(frozen=True)
class WorkerPolicy:
    schema_version: int
    repository_id: str
    run_id: str
    activity_id: str
    attempt_id: str
    generation: int
    workspace: str
    read_only_roots: tuple[str, ...]
    writable_roots: tuple[str, ...]
    ipc_endpoint: str
    network_policy: dict[str, str]
    sandbox_layers: dict[str, str]
    composition_evidence_hash: str
    policy_sha256: str


@dataclass(frozen=True)
class ArtifactReviewPolicy:
    """No-IPC, offline, read-only artifact reviewer policy for one attempt."""
    schema_version: int
    repository_id: str
    run_id: str
    activity_id: str
    attempt_id: str
    generation: int
    artifact_root: str
    read_only_roots: tuple[str, ...]
    writable_roots: tuple[str, ...]
    executable: str
    ipc_endpoint: None
    runtime_tuple_hash: str
    manifest_sha256: str
    network_policy: dict[str, str]
    sandbox_layers: dict[str, str]
    composition_evidence_hash: str
    policy_sha256: str


_HEX = frozenset("0123456789abcdef")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX for char in value.lower())


def _canonical_directory(value: object) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT")
    raw = Path(value)
    if not raw.is_absolute():
        raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT")
    try:
        # Policy roots are concrete, pre-registered directories.  Resolving a
        # symlink would turn an operator typo into an unreviewed grant.
        info = raw.lstat()
        resolved = raw.resolve(strict=True)
        # `lstat` protects the leaf; equality also rejects a symlinked
        # ancestor such as /var -> /private/var.  Registrations must carry
        # the explicit canonical path, rather than an alias to an allowed
        # directory.
        if (
            raw.is_symlink() or not raw.is_dir() or not os.path.normpath(value) == value
            or resolved != raw
        ):
            raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT")
        if not resolved.is_dir() or resolved.stat().st_dev != info.st_dev:
            raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT")
        return resolved
    except (OSError, RuntimeError):
        raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT") from None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _same_or_overlap(first: Path, second: Path) -> bool:
    return _inside(first, second) or _inside(second, first)


def _require_identity(context, registration: WorkerRegistration, roots: WorkerRuntimeRoots) -> None:
    pairs = (
        ("repository_id", context.repository_id, registration.repository_id),
        ("run_id", context.run_id, registration.run_id),
        ("activity_id", context.activity_id, registration.activity_id),
        ("attempt_id", context.attempt_id, registration.attempt_id),
        ("generation", context.generation, registration.generation),
    )
    if any(left != right for _name, left, right in pairs):
        raise WorkerPolicyRefused("WORKER_POLICY_MISMATCH")
    if (
        not isinstance(registration.generation, int) or isinstance(registration.generation, bool)
        or registration.generation < 1
        or not _is_hash(context.runtime_tuple_hash)
        or context.runtime_tuple_hash != roots.runtime_tuple_hash
        or not _is_hash(roots.manifest_sha256)
        or not _is_hash(registration.composition_evidence_hash)
    ):
        raise WorkerPolicyRefused("WORKER_POLICY_MISMATCH")


def _endpoint(registration: WorkerRegistration, socket_root: Path) -> str:
    endpoint = Path(registration.ipc_endpoint)
    if (
        not endpoint.is_absolute() or not endpoint.name or endpoint.name in {".", ".."}
        or endpoint.parent != socket_root or os.path.normpath(registration.ipc_endpoint) != registration.ipc_endpoint
    ):
        raise WorkerPolicyRefused("UNSAFE_IPC_ENDPOINT")
    # A pre-existing endpoint must be a socket owned by this uid; ordinary
    # files and symlinks cannot be substituted as a supervisor channel.
    if endpoint.exists() or endpoint.is_symlink():
        try:
            info = endpoint.lstat()
        except OSError:
            raise WorkerPolicyRefused("UNSAFE_IPC_ENDPOINT") from None
        if endpoint.is_symlink() or info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise WorkerPolicyRefused("UNSAFE_IPC_ENDPOINT")
    return str(endpoint)


def _unsigned_policy(policy: WorkerPolicy) -> dict:
    value = asdict(policy)
    value.pop("policy_sha256", None)
    return value


def _assert_policy_hash(policy: WorkerPolicy) -> None:
    if not isinstance(policy, (WorkerPolicy, ArtifactReviewPolicy)):
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
    encoded = hashlib.sha256(_canonical(_unsigned_policy(policy))).hexdigest()
    if not hmac.compare_digest(encoded, policy.policy_sha256):
        raise WorkerPolicyRefused("POLICY_HASH_MISMATCH")


def _review_protected(registration: WorkerRegistration, workspace: Path) -> tuple[Path, ...]:
    return (
        workspace, _canonical_directory(registration.primary_root),
        _canonical_directory(registration.state_root), _canonical_directory(registration.git_common_dir),
        _canonical_directory(registration.socket_root), _canonical_directory(registration.policy_root),
        *(_canonical_directory(item) for item in registration.sibling_roots),
    )


def build_artifact_review_policy(context, registration: WorkerRegistration,
                                 roots: ArtifactReviewRoots) -> ArtifactReviewPolicy:
    """Build a closed read-only artifact reviewer policy without worker IPC.

    This remains a policy constructor only.  It does not admit a native host
    or supply artifact bytes to one.
    """
    runtime = WorkerRuntimeRoots(roots.runtime_read_only_roots, roots.attempt_scratch,
                                 roots.runtime_tuple_hash, roots.manifest_sha256)
    _require_identity(context, registration, runtime)
    workspace = _canonical_directory(context.workspace)
    if workspace != _canonical_directory(registration.workspace):
        raise WorkerPolicyRefused("WORKER_POLICY_MISMATCH")
    artifact = _canonical_directory(roots.artifact_root)
    runtime_roots = tuple(_canonical_directory(item) for item in roots.runtime_read_only_roots)
    scratch = _canonical_directory(roots.attempt_scratch)
    if not runtime_roots or len(set(runtime_roots)) != len(runtime_roots):
        raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT")
    protected = _review_protected(registration, workspace)
    # Review runtime and artifact roots may not be inside HOME.  This is
    # deliberately conservative until a dedicated non-home runtime profile
    # is reviewed; it prevents an approved recursive read from covering user
    # configuration or credentials by placement alone.
    home = _canonical_directory(str(Path.home()))
    readonly = (artifact, *runtime_roots)
    if (
        any(_same_or_overlap(root, protected_root) for root in readonly for protected_root in protected)
        or any(_same_or_overlap(root, home) for root in readonly)
        or any(_same_or_overlap(scratch, root) for root in (*protected, home, *readonly))
        or any(_same_or_overlap(artifact, root) for root in runtime_roots)
    ):
        raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT")
    executable_raw = Path(roots.executable)
    if not executable_raw.is_absolute() or "\0" in roots.executable:
        raise WorkerPolicyRefused("UNSAFE_REVIEW_EXECUTABLE")
    try:
        executable = executable_raw.resolve(strict=True)
        info = executable.stat()
    except OSError:
        raise WorkerPolicyRefused("UNSAFE_REVIEW_EXECUTABLE") from None
    if not stat.S_ISREG(info.st_mode) or not any(_inside(executable, root) for root in runtime_roots):
        raise WorkerPolicyRefused("UNSAFE_REVIEW_EXECUTABLE")
    unsigned = {
        "schema_version": 1, "repository_id": registration.repository_id, "run_id": registration.run_id,
        "activity_id": registration.activity_id, "attempt_id": registration.attempt_id,
        "generation": registration.generation, "artifact_root": str(artifact),
        "read_only_roots": tuple(str(item) for item in readonly), "writable_roots": (str(scratch),),
        "executable": str(executable), "ipc_endpoint": None,
        "runtime_tuple_hash": roots.runtime_tuple_hash, "manifest_sha256": roots.manifest_sha256,
        "network_policy": {"native_network": "denied", "transport": "offline-only"},
        "sandbox_layers": {"filesystem": "required", "network": "required", "ipc": "none",
                           "process_exec": "exact-executable", "process_fork": "denied"},
        "composition_evidence_hash": registration.composition_evidence_hash,
    }
    return ArtifactReviewPolicy(**unsigned, policy_sha256=hashlib.sha256(_canonical(unsigned)).hexdigest())


def build_worker_policy(context, registration: WorkerRegistration, roots: WorkerRuntimeRoots) -> WorkerPolicy:
    """Validate and hash the complete root/identity policy for one attempt."""
    _require_identity(context, registration, roots)
    workspace = _canonical_directory(context.workspace)
    registered_workspace = _canonical_directory(registration.workspace)
    if workspace != registered_workspace:
        raise WorkerPolicyRefused("WORKER_POLICY_MISMATCH")
    primary = _canonical_directory(registration.primary_root)
    state = _canonical_directory(registration.state_root)
    common = _canonical_directory(registration.git_common_dir)
    socket_root = _canonical_directory(registration.socket_root)
    policy_root = _canonical_directory(registration.policy_root)
    siblings = tuple(_canonical_directory(item) for item in registration.sibling_roots)
    scratch = _canonical_directory(roots.attempt_scratch)
    read_only = tuple(_canonical_directory(item) for item in roots.read_only_roots)
    if not read_only or len(set(read_only)) != len(read_only):
        raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT")

    protected = (primary, state, common, socket_root, policy_root, *siblings)
    # No writable root may name a protected surface or a parent which would
    # grant that surface indirectly.  Scratch additionally may not live in a
    # registered workspace; each attempt receives a distinct owned directory.
    if (
        any(_same_or_overlap(workspace, root) for root in protected)
        or any(_same_or_overlap(scratch, root) for root in (*protected, workspace))
        or any(_same_or_overlap(root, writable) for root in read_only for writable in (workspace, scratch))
    ):
        raise WorkerPolicyRefused("UNSAFE_WORKER_ROOT")
    endpoint = _endpoint(registration, socket_root)

    unsigned = {
        "schema_version": 1,
        "repository_id": registration.repository_id,
        "run_id": registration.run_id,
        "activity_id": registration.activity_id,
        "attempt_id": registration.attempt_id,
        "generation": registration.generation,
        "workspace": str(workspace),
        "read_only_roots": tuple(str(item) for item in read_only),
        "writable_roots": (str(workspace), str(scratch)),
        "ipc_endpoint": endpoint,
        "network_policy": {
            "model_egress": "supervisor-only",
            "shell_network": "denied",
            "native_network": "denied",
        },
        "sandbox_layers": {
            "filesystem": "required",
            "shell_network": "required",
            "native_tools": "required",
            # macOS enforces this using process-fork; Linux has no measured
            # equivalent in the present backend and is refused below.
            "nesting": "platform-qualified",
        },
        "composition_evidence_hash": registration.composition_evidence_hash,
    }
    return WorkerPolicy(**unsigned, policy_sha256=hashlib.sha256(_canonical(unsigned)).hexdigest())


def _darwin_profile(policy: WorkerPolicy) -> str:
    read_rules = " ".join(
        f'(subpath {json.dumps(root)})' for root in (*policy.read_only_roots, *policy.writable_roots)
    )
    write_rules = " ".join(f'(subpath {json.dumps(root)})' for root in policy.writable_roots)
    return "\n".join((
        "(version 1)", "(deny default)",
        f"(allow file-read* {read_rules} (literal {json.dumps(policy.ipc_endpoint)}))",
        f"(allow file-write* {write_rules} (literal {json.dumps(policy.ipc_endpoint)}))",
        # Runtime roots must explicitly contain the executable and its
        # libraries.  There is intentionally no broad system profile here.
        "(allow process-exec)", "(deny network*)", "(deny process-fork)",
    ))


def _darwin_artifact_review_profile(policy: ArtifactReviewPolicy) -> str:
    readable_roots = (*policy.read_only_roots, *policy.writable_roots)
    read_rules = " ".join(f'(subpath {json.dumps(root)})' for root in readable_roots)
    # A process must traverse the named paths, but metadata access to a
    # parent must not become a recursive filesystem-read grant.  Use exact
    # ancestor literals for that platform requirement and keep data reads
    # confined to the approved roots.
    ancestors = {"/"}
    for root in readable_roots:
        path = Path(root)
        while True:
            ancestors.add(str(path))
            if path.parent == path:
                break
            path = path.parent
    metadata_rules = " ".join(
        f'(literal {json.dumps(root)})' for root in sorted(ancestors)
    )
    write_rules = " ".join(f'(subpath {json.dumps(root)})' for root in policy.writable_roots)
    return "\n".join((
        "(version 1)", "(deny default)",
        # Darwin's loader opens selected path ancestors while resolving the
        # executable.  These are exact literals only; recursive reads remain
        # limited to the reviewed roots above.
        f"(allow file-read* {metadata_rules} {read_rules})",
        f"(allow file-write* {write_rules})",
        f"(allow process-exec (literal {json.dumps(policy.executable)}))",
        "(deny process-fork)", "(deny network*)",
    ))


def build_contained_argv(policy: WorkerPolicy, argv: tuple[str, ...], *, platform: str | None = None) -> list[str]:
    """Return the required native containment argv, or refuse.

    The returned argv intentionally names the platform binary directly.  A
    missing binary is a launch-time containment failure, never permission to
    use ``argv`` unwrapped.
    """
    if not isinstance(policy, WorkerPolicy):
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
    _assert_policy_hash(policy)
    if not argv or not all(
        isinstance(item, str) and item and "\0" not in item for item in argv
    ):
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return ["/usr/bin/sandbox-exec", "-p", _darwin_profile(policy), "--", *argv]
    if platform.startswith("linux") or platform == "linux":
        # bwrap can give us filesystem and network isolation, but the present
        # invocation cannot prove denial of nested agent processes.  Refuse
        # this backend until a measured seccomp/nesting profile is available.
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
    raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")


def build_artifact_review_argv(policy: ArtifactReviewPolicy, argv: tuple[str, ...], *,
                              platform: str | None = None) -> list[str]:
    """Wrap exactly the reviewer executable; refuse all other launch chains."""
    if not isinstance(policy, ArtifactReviewPolicy):
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
    _assert_policy_hash(policy)
    if (not argv
            or not all(isinstance(item, str) and item and "\0" not in item for item in argv)):
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
    try:
        executable = str(Path(argv[0]).resolve(strict=True))
    except OSError:
        raise WorkerPolicyRefused("UNSAFE_REVIEW_EXECUTABLE") from None
    if not hmac.compare_digest(executable, policy.executable):
        raise WorkerPolicyRefused("UNSAFE_REVIEW_EXECUTABLE")
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        if not os.path.isfile("/usr/bin/sandbox-exec"):
            raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
        return ["/usr/bin/sandbox-exec", "-p", _darwin_artifact_review_profile(policy), "--", *argv]
    # No Linux backend is claimed until an actual enforced profile exists.
    raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")


def smoke_containment(policy: WorkerPolicy, *, timeout_seconds: float = 5.0) -> None:
    """Execute a harmless native boundary probe for a qualified policy.

    This is intentionally an execution check, not a syntax check.  A caller
    must run and record it for its platform/runtime tuple before dispatching a
    contained worker.
    """
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
    platform = sys.platform
    if platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"):
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
    command = ("/usr/bin/true",)
    try:
        completed = subprocess.run(
            build_contained_argv(policy, command, platform=platform),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_seconds, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE") from None
    if completed.returncode != 0:
        raise WorkerPolicyRefused("CONFINEMENT_UNAVAILABLE")
