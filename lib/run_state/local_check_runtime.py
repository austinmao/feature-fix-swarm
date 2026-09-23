"""Typed, sealed local-check launch material.

This is deliberately a small transport boundary.  It is not a host
qualification and it cannot manufacture a general-purpose child command: the
only constructor resolves a command from an already sealed acceptance check.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile


class LocalCheckRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _file_digest(path: Path, *, system_executable=False) -> str:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat(follow_symlinks=False)
        system_link = (system_executable and info.st_uid == 0 and not info.st_mode & 0o022
                       and resolved.parent in {Path('/usr/bin'), Path('/bin')})
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 and not system_link:
            raise LocalCheckRefused("LOCAL_CHECK_SOURCE_INVALID")
        with resolved.open("rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()
    except OSError as error:
        raise LocalCheckRefused("LOCAL_CHECK_SOURCE_INVALID") from error


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _resolved_source_closure(argv: tuple[str, ...], workspace: Path) -> tuple[tuple[str, str], ...]:
    """Capture file operands which the local command will read from the child cwd.

    Literal arguments (including ``-c`` source) remain in ``argv_sha256``.
    Existing file operands are captured separately so changing a script after
    reservation cannot inherit the permit.
    """
    sources: list[tuple[str, str]] = []
    for argument in argv[1:]:
        candidate = Path(argument)
        path = candidate if candidate.is_absolute() else workspace / candidate
        try:
            exists = path.exists()
        except OSError as error:
            raise LocalCheckRefused("LOCAL_CHECK_SOURCE_INVALID") from error
        if not exists:
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise LocalCheckRefused("LOCAL_CHECK_SOURCE_INVALID") from error
        if workspace != resolved and workspace not in resolved.parents:
            # A check may run an installed executable, but checked source must
            # be a registered workspace input rather than a caller-owned path.
            raise LocalCheckRefused("LOCAL_CHECK_SOURCE_INVALID")
        sources.append((str(resolved.relative_to(workspace)), _file_digest(resolved)))
    return tuple(sorted(set(sources)))


@dataclass(frozen=True)
class LocalCheckMaterial:
    schema: str
    acceptance_hash: str
    check_id: str
    locator: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    executable: str
    executable_sha256: str
    source_closure: tuple[tuple[str, str], ...]
    argv_sha256: str
    source_closure_sha256: str
    candidate_hash: str
    workspace: str
    workspace_preparation_id: str
    expected_head: str
    runtime_identity: str
    generation: int
    confinement_policy_sha256: str = ""
    confinement_scratch: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def execution_environment(self) -> dict[str, str]:
        return dict(self.environment)

    @property
    def material_sha256(self) -> str:
        return digest(self.to_dict())


def sealed_check_material(*, sealed: object, acceptance_hash: str, check_id: str,
                          candidate_hash: str, workspace: str, workspace_preparation_id: str,
                          expected_head: str, runtime_identity: str, generation: int) -> LocalCheckMaterial:
    """Resolve exactly one sealed command check into closed launch material."""
    if (not all(_is_digest(item) for item in (acceptance_hash, candidate_hash, runtime_identity))
            or not isinstance(check_id, str) or not check_id
            or not isinstance(workspace_preparation_id, str) or not workspace_preparation_id
            or not isinstance(expected_head, str) or not expected_head
            or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1):
        raise LocalCheckRefused("LOCAL_CHECK_MATERIAL_INVALID")
    material = getattr(sealed, "material", None)
    if getattr(sealed, "acceptance_hash", None) != acceptance_hash or not isinstance(material, dict):
        raise LocalCheckRefused("LOCAL_CHECK_SEAL_STALE")
    matches = [check for criterion in material.get("criteria", []) if isinstance(criterion, dict)
               for check in criterion.get("checks", []) if isinstance(check, dict) and check.get("id") == check_id]
    if len(matches) != 1 or matches[0].get("kind") != "command" or not isinstance(matches[0].get("locator"), str):
        raise LocalCheckRefused("LOCAL_CHECK_SEAL_INVALID")
    locator = matches[0]["locator"]
    try:
        raw_argv = tuple(shlex.split(locator, posix=True))
    except ValueError as error:
        raise LocalCheckRefused("LOCAL_CHECK_SEAL_INVALID") from error
    if not raw_argv or not all(isinstance(item, str) and item and "\0" not in item for item in raw_argv):
        raise LocalCheckRefused("LOCAL_CHECK_SEAL_INVALID")
    executable = Path(raw_argv[0])
    if not executable.is_absolute():
        raise LocalCheckRefused("LOCAL_CHECK_SEAL_INVALID")
    try:
        executable = executable.resolve(strict=True)
    except OSError as error:
        raise LocalCheckRefused("LOCAL_CHECK_SOURCE_INVALID") from error
    argv = (str(executable), *raw_argv[1:])
    workspace_path = Path(workspace)
    try:
        if (not workspace_path.is_absolute() or workspace_path.resolve(strict=True) != workspace_path
                or not workspace_path.is_dir()):
            raise LocalCheckRefused("LOCAL_CHECK_WORKSPACE_INVALID")
    except OSError as error:
        raise LocalCheckRefused("LOCAL_CHECK_WORKSPACE_INVALID") from error
    # No inherited configuration, credentials, control DB, or user PATH.
    environment = (("HOME", str(workspace_path)), ("LANG", "C"), ("LC_ALL", "C"),
                   ("PATH", "/usr/bin:/bin"))
    closure = _resolved_source_closure(argv, workspace_path)
    return LocalCheckMaterial(
        "ffs.local-sealed-check/v1", acceptance_hash, check_id, locator, argv, environment,
        str(executable), _file_digest(executable, system_executable=True), closure, digest(argv), digest(closure), candidate_hash,
        str(workspace_path), workspace_preparation_id, expected_head, runtime_identity, generation,
    )


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def verify_local_candidate(store, material):
    from .workspace import inspect_workspace, _verify_snapshot_complete, WorkspaceRefused
    from .wave_execution import _inventory, _material_entries
    try:
        preparation = inspect_workspace(store, material.workspace_preparation_id)
        _verify_snapshot_complete(store, preparation, verify_workspace=True)
        manifest = json.loads(preparation.selected_manifest_json)
        expected = tuple(sorted(manifest['entries'], key=lambda entry: entry['path']))
        actual = _material_entries(preparation.path, preparation.base_commit,
                                   _inventory(preparation.path, preparation.base_commit))
        if expected != actual:
            raise LocalCheckRefused('LOCAL_CHECK_CANDIDATE_STALE')
    except (WorkspaceRefused, KeyError, ValueError) as error:
        raise LocalCheckRefused('LOCAL_CHECK_CANDIDATE_STALE') from error


def build_confined_local_argv(store, token, activity_id: str, material: LocalCheckMaterial):
    """Build a directly-bound Darwin artifact policy for one registered child.

    The reviewed renderer is reused, while this constructor admits only the
    exact registered child workspace (including a descendant of HOME), never
    HOME or a general user root.
    """
    from run_state.worker_policy import ArtifactReviewPolicy, WorkerPolicyRefused, build_artifact_review_argv
    if sys.platform != "darwin" or not os.path.isfile("/usr/bin/sandbox-exec"):
        raise LocalCheckRefused("LOCAL_CHECK_CONFINEMENT_UNAVAILABLE")
    with store.read_transaction() as tx:
        child = tx.execute("SELECT * FROM authority_child_bindings WHERE activity_id=?", (activity_id,)).fetchone()
        workspace = tx.execute("SELECT path,state,generation,created_by_ffs,repository_path,common_dir FROM context_workspaces WHERE preparation_id=?", (material.workspace_preparation_id,)).fetchone()
    if (child is None or workspace is None or child["workspace_binding"] != material.workspace
            or workspace["path"] != material.workspace or workspace["state"] != "ready"
            or not workspace["created_by_ffs"] or workspace["generation"] != token.generation
            or child["candidate_hash"] != material.candidate_hash or child["runtime_identity"] != material.runtime_identity):
        raise LocalCheckRefused("LOCAL_CHECK_BINDING_INVALID")
    artifact = Path(material.workspace)
    primary, state = Path(workspace['repository_path']), Path(store.db_path).parent
    try:
        common = Path(workspace['common_dir']).resolve(strict=True)
        runtime = tuple(dict.fromkeys((Path(material.executable).parent, Path("/usr/lib"), Path("/System/Library"))))
        if any(not root.is_dir() for root in runtime):
            raise LocalCheckRefused("LOCAL_CHECK_CONFINEMENT_UNAVAILABLE")
        if any(_overlap(root, blocked) for root in runtime for blocked in (primary, state, common, Path.home())):
            raise LocalCheckRefused("LOCAL_CHECK_CONFINEMENT_INVALID")
        if any(_overlap(artifact, blocked) for blocked in (primary, state, common)):
            raise LocalCheckRefused('LOCAL_CHECK_CONFINEMENT_INVALID')
        scratch = Path(tempfile.mkdtemp(prefix="ffs-local-check-")).resolve(strict=True)
        if any(_overlap(scratch, blocked) for blocked in (artifact, primary, state, common, Path.home())):
            raise LocalCheckRefused("LOCAL_CHECK_CONFINEMENT_INVALID")
    except OSError as error:
        raise LocalCheckRefused("LOCAL_CHECK_CONFINEMENT_UNAVAILABLE") from error
    unsigned = {
        "schema_version": 1, "repository_id": token.repository_id, "run_id": token.run_id,
        "activity_id": activity_id, "attempt_id": "local-check:" + material.material_sha256,
        "generation": token.generation, "artifact_root": str(artifact),
        "read_only_roots": tuple(str(item) for item in (artifact, *runtime)),
        "writable_roots": (str(scratch),), "executable": material.executable, "ipc_endpoint": None,
        "runtime_tuple_hash": material.runtime_identity, "manifest_sha256": material.source_closure_sha256,
        "network_policy": {"native_network": "denied", "transport": "offline-only"},
        "sandbox_layers": {"filesystem": "required", "network": "required", "ipc": "none",
                           "process_exec": "exact-executable", "process_fork": "denied"},
        "composition_evidence_hash": material.material_sha256,
    }
    policy = ArtifactReviewPolicy(**unsigned, policy_sha256=digest(unsigned))
    bound = replace(material, environment=(("HOME", str(scratch)), ("LANG", "C"), ("LC_ALL", "C"),
                                             ("PATH", "/usr/bin:/bin")),
                    confinement_policy_sha256=policy.policy_sha256, confinement_scratch=str(scratch))
    try:
        return bound, tuple(build_artifact_review_argv(policy, bound.argv)), policy
    except WorkerPolicyRefused as error:
        raise LocalCheckRefused(error.code) from error


def validate_local_check_material(value: object, *, sealed: object | None = None,
                                  require_current_bytes: bool = False) -> LocalCheckMaterial:
    if type(value) is not LocalCheckMaterial:
        raise LocalCheckRefused("LOCAL_CHECK_MATERIAL_INVALID")
    material = value
    if (material.schema != "ffs.local-sealed-check/v1" or not all(_is_digest(item) for item in (
            material.acceptance_hash, material.executable_sha256, material.argv_sha256,
            material.source_closure_sha256, material.candidate_hash, material.runtime_identity))
            or not isinstance(material.check_id, str) or not material.check_id
            or not isinstance(material.locator, str) or not material.locator
            or not isinstance(material.workspace_preparation_id, str) or not material.workspace_preparation_id
            or not isinstance(material.expected_head, str) or not material.expected_head
            or isinstance(material.generation, bool) or material.generation < 1
            or (material.confinement_policy_sha256 and not _is_digest(material.confinement_policy_sha256))):
        raise LocalCheckRefused("LOCAL_CHECK_MATERIAL_INVALID")
    try:
        locator_argv = tuple(shlex.split(material.locator, posix=True))
        resolved_argv = ((str(Path(locator_argv[0]).resolve(strict=True)) if sealed is not None or require_current_bytes
                          else material.executable), *locator_argv[1:])
    except (OSError, ValueError, IndexError) as error:
        raise LocalCheckRefused("LOCAL_CHECK_MATERIAL_INVALID") from error
    if (resolved_argv != material.argv
            or digest(material.argv) != material.argv_sha256
            or tuple(sorted(material.environment)) != material.environment
            or set(dict(material.environment)) != {"HOME", "LANG", "LC_ALL", "PATH"}
            or dict(material.environment)["LANG"] != "C" or dict(material.environment)["LC_ALL"] != "C"
            or dict(material.environment)["PATH"] != "/usr/bin:/bin"
            or material.executable != material.argv[0] or not Path(material.executable).is_absolute()
            or digest(material.source_closure) != material.source_closure_sha256):
        raise LocalCheckRefused("LOCAL_CHECK_MATERIAL_INVALID")
    workspace = Path(material.workspace)
    if not workspace.is_absolute() or str(workspace) != material.workspace:
        raise LocalCheckRefused("LOCAL_CHECK_WORKSPACE_INVALID")
    if sealed is not None:
        expected = sealed_check_material(
            sealed=sealed, acceptance_hash=material.acceptance_hash, check_id=material.check_id,
            candidate_hash=material.candidate_hash, workspace=material.workspace,
            workspace_preparation_id=material.workspace_preparation_id, expected_head=material.expected_head,
            runtime_identity=material.runtime_identity, generation=material.generation,
        )
        if replace(expected, environment=material.environment,
                   confinement_policy_sha256=material.confinement_policy_sha256,
                   confinement_scratch=material.confinement_scratch) != material:
            raise LocalCheckRefused("LOCAL_CHECK_MATERIAL_STALE")
    elif require_current_bytes:
        if _file_digest(Path(material.executable), system_executable=True) != material.executable_sha256 or _resolved_source_closure(material.argv, workspace) != material.source_closure:
            raise LocalCheckRefused("LOCAL_CHECK_MATERIAL_STALE")
    return material
