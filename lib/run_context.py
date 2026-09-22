"""Canonical fixture run context and repository identity resolution."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

import fcntl


_RUN_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$")
_GIT_TIMEOUT = 15.0
_EMPTY_SELECTION_MANIFEST_HASH = hashlib.sha256(b'{"entries":[]}').hexdigest()


class ContextRefused(RuntimeError):
    def __init__(self, code: str, *, candidates: list[dict] | None = None) -> None:
        self.code = code
        self.candidates = candidates or []
        super().__init__(code)


class InvalidRunId(ContextRefused):
    def __init__(self) -> None:
        super().__init__("INVALID_RUN_ID")


@dataclass(frozen=True)
class RepositoryDescriptor:
    checkout: Path
    common_dir: Path
    primary_root: Path
    filesystem_id: str
    head: str


@dataclass(frozen=True)
class GitAdminLockHandles:
    """Descriptors held for one repository-administration transaction."""

    lock_fd: int
    directory_fd: int


@dataclass(frozen=True)
class ContextRequest:
    cwd: Path
    operation: str
    objective: str = ""
    explicit_run_id: str | None = None
    activity: str | None = None
    planning_scope: str = ""
    resume: bool = False
    revise: bool = False
    request_key: str | None = None
    selected_inputs: tuple[str, ...] = ()
    inherited: Mapping[str, str] | None = None
    minted_run_id: bool = False


@dataclass(frozen=True)
class RunContext:
    repository_id: str
    run_id: str
    activity_id: str
    workspace: str
    evidence_root: str
    generation: int
    workspace_state: str
    ready: bool
    selected_input_manifest_hash: str
    selection_manifest_hash: str = _EMPTY_SELECTION_MANIFEST_HASH
    input_digest: str = _EMPTY_SELECTION_MANIFEST_HASH
    selected_input_count: int = 0
    attempt_id: str | None = None
    runtime_tuple_hash: str | None = None
    reused_result: bool = False
    result: dict | None = None
    upstream: dict[str, str | None] | None = None

    def as_payload(self, *, code: str = "RUN_READY") -> dict:
        payload = asdict(self)
        payload.update(schema_version=1, ok=True, code=code, candidates=[])
        return payload


def validate_run_id(raw: str) -> str:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64 or not _RUN_ID.fullmatch(raw):
        raise InvalidRunId()
    return raw


def select_run_id(explicit: str | None, inherited: Mapping[str, str]) -> str:
    aliases = [value for value in (inherited.get("GSD_RUN_ID"), inherited.get("FFS_RUN_ID")) if value]
    for value in aliases:
        validate_run_id(value)
    if len(set(aliases)) > 1:
        raise ContextRefused("CONFLICTING_RUN_ID")
    if explicit is not None:
        validate_run_id(explicit)
        if aliases and aliases[0] != explicit:
            raise ContextRefused("CONFLICTING_RUN_ID")
        return explicit
    if aliases:
        return aliases[0]
    return f"adhoc-{uuid.uuid4().hex}"


def objective_digest(objective: str) -> str:
    return hashlib.sha256(objective.encode("utf-8")).hexdigest()


def request_digest(request: ContextRequest, run_id: str) -> str:
    value = {
        "run_id": None if request.minted_run_id else run_id,
        "operation": request.operation,
        "objective": request.objective,
        "activity": request.activity,
        "planning_scope": request.planning_scope,
        "resume": request.resume,
        "revise": request.revise,
        "selected_inputs": list(request.selected_inputs),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sanitized_git_environment() -> dict[str, str]:
    """Retain ordinary process context while removing Git routing/config injection."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
    )
    return env


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, env=sanitized_git_environment(),
            capture_output=True, text=True,
            timeout=_GIT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ContextRefused("REPOSITORY_UNAVAILABLE") from error
    if result.returncode:
        raise ContextRefused("REPOSITORY_UNAVAILABLE")
    return result.stdout.strip()


def resolve_repository(cwd: Path) -> RepositoryDescriptor:
    """Resolve linked checkouts without writing Git or control state."""
    checkout = Path(_git(Path(cwd), "rev-parse", "--show-toplevel")).resolve(strict=True)
    common_raw = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common_dir = Path(common_raw).resolve(strict=True)
    records = _git(checkout, "worktree", "list", "--porcelain").splitlines()
    # Git retains prunable registrations as recovery evidence. An unrelated
    # missing sibling must not gate discovery of the current repository.
    roots = [Path(line[9:]) for line in records if line.startswith("worktree ")]
    if not roots:
        raise ContextRefused("REPOSITORY_UNAVAILABLE")
    info = common_dir.stat()
    try:
        fsid = int(os.statvfs(common_dir).f_fsid)
    except (AttributeError, OSError) as error:
        raise ContextRefused("REPOSITORY_UNAVAILABLE") from error
    return RepositoryDescriptor(
        checkout=checkout,
        common_dir=common_dir,
        primary_root=roots[0].resolve(strict=True),
        filesystem_id=f"{info.st_dev}:{fsid}",
        head=_git(checkout, "rev-parse", "HEAD"),
    )


def validate_state_root(state_root: Path, repository: RepositoryDescriptor) -> Path:
    root = Path(state_root)
    if not root.is_absolute() or os.path.normpath(os.fspath(root)) != os.fspath(root):
        raise ContextRefused("UNSAFE_STATE_ROOT")
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_symlink():
        raise ContextRefused("UNSAFE_STATE_ROOT")
    try:
        canonical = probe.resolve(strict=True).joinpath(*root.relative_to(probe).parts)
    except (OSError, ValueError) as error:
        raise ContextRefused("UNSAFE_STATE_ROOT") from error
    planned_workspace_root = repository.primary_root.parent / ".ffs-workspaces"
    protected = [
        repository.common_dir, repository.primary_root, repository.checkout,
        planned_workspace_root,
    ]
    roots = _git(repository.checkout, "worktree", "list", "--porcelain").splitlines()
    protected.extend(Path(line[9:]).resolve() for line in roots if line.startswith("worktree "))
    for item in protected:
        try:
            if os.path.commonpath((canonical, item)) == os.fspath(item):
                raise ContextRefused("UNSAFE_STATE_ROOT")
        except ValueError:
            continue
    return canonical


@contextmanager
def git_admin_lock(common_dir: Path, *, timeout: float = 15.0):
    """Hold one repository admin flock; children may inherit its descriptor."""
    with _repository_effect_lock(common_dir, "admin.lock", timeout=timeout) as handles:
        yield handles


@contextmanager
def workspace_effect_lock(common_dir: Path, *, repository_id: str, run_id: str,
                          preparation_id: str, timeout: float = 15.0):
    """Serialize effects for one immutable preparation across owner generations."""
    key = hashlib.sha256(json.dumps([repository_id, run_id, preparation_id],
                                  separators=(",", ":")).encode()).hexdigest()
    with _repository_effect_lock(common_dir, "workspace-" + key + ".lock",
                                 timeout=timeout) as handles:
        yield handles


@contextmanager
def _repository_effect_lock(common_dir: Path, name: str, *, timeout: float):
    owned_dir = common_dir / "ffs"
    try:
        owned_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise ContextRefused("REPOSITORY_IDENTITY_INVALID") from error
    directory_fd = -1
    try:
        directory_fd = os.open(
            owned_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ContextRefused("REPOSITORY_IDENTITY_INVALID") from error
    info = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        os.close(directory_fd)
        raise ContextRefused("REPOSITORY_IDENTITY_INVALID")
    try:
        # Older FFS writers created this directory as 0755. Tightening a real,
        # current-user-owned, non-writable legacy directory preserves its state
        # while satisfying the new private authority boundary.
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(directory_fd, 0o700)
            os.fsync(directory_fd)
        current = owned_dir.lstat()
        if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or not stat.S_ISDIR(current.st_mode)
                or current.st_uid != os.getuid()
                or stat.S_IMODE(current.st_mode) != 0o700):
            raise ContextRefused("REPOSITORY_IDENTITY_INVALID")
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
    except BaseException:
        os.close(directory_fd)
        raise
    lock_info = os.fstat(fd)
    if (
        not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
        or lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) & 0o077
    ):
        os.close(fd)
        os.close(directory_fd)
        raise ContextRefused("REPOSITORY_IDENTITY_INVALID")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ContextRefused("GIT_ADMIN_BUSY")
                time.sleep(0.02)
        yield GitAdminLockHandles(lock_fd=fd, directory_fd=directory_fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            os.close(directory_fd)


def _validate_authority_directory(directory_fd: int) -> None:
    info = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ContextRefused("REPOSITORY_IDENTITY_INVALID")


def _read_marker(path: Path, *, directory_fd: int | None = None) -> str | None:
    owned_directory_fd = directory_fd is None
    try:
        if directory_fd is None:
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        _validate_authority_directory(directory_fd)
    except FileNotFoundError:
        return None
    except ContextRefused:
        if owned_directory_fd and directory_fd is not None:
            os.close(directory_fd)
        raise
    except OSError as error:
        if owned_directory_fd and directory_fd is not None:
            os.close(directory_fd)
        raise ContextRefused("REPOSITORY_IDENTITY_INVALID") from error
    marker_fd = -1
    try:
        try:
            marker_fd = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        info = os.fstat(marker_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ContextRefused("REPOSITORY_IDENTITY_INVALID")
        payload = bytearray()
        while len(payload) <= 4096:
            chunk = os.read(marker_fd, 4097 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > 4096:
            raise ValueError
        value = json.loads(payload.decode("utf-8"))
        repository_id = value["repository_id"]
        if value.get("schema_version") != 1 or str(uuid.UUID(repository_id)) != repository_id:
            raise ValueError
    except ContextRefused:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise ContextRefused("REPOSITORY_IDENTITY_INVALID") from error
    finally:
        if marker_fd >= 0:
            os.close(marker_fd)
        if owned_directory_fd and directory_fd is not None:
            os.close(directory_fd)
    return repository_id


def _unregistered_repository_identity(repository: RepositoryDescriptor) -> str:
    """Provide a stable, non-authoritative identity before registration.

    This value supports read-only discovery only.  It must never be persisted
    as a repository marker or accepted at a selected-input capture boundary.
    """
    try:
        info = repository.common_dir.stat()
    except OSError as error:
        raise ContextRefused("REPOSITORY_UNAVAILABLE") from error
    material = f"ffs-repository:{repository.common_dir}:{info.st_dev}:{info.st_ino}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material))


def repository_identity(repository: RepositoryDescriptor) -> str:
    """Return a marker identity when present, otherwise a read-only provisional ID."""
    value = _read_marker(repository.common_dir / "ffs" / "repository.json")
    return value if value is not None else _unregistered_repository_identity(repository)


def registered_repository_identity(repository: RepositoryDescriptor) -> str:
    """Return the durable marker ID without creating any repository state."""
    value = _read_marker(repository.common_dir / "ffs" / "repository.json")
    if value is None:
        raise ContextRefused("REPOSITORY_NOT_REGISTERED")
    return value


def register_repository(store, repository: RepositoryDescriptor, state_root: Path) -> str:
    """Bind a durable UUID to this Git common directory and fixture store."""
    marker = repository.common_dir / "ffs" / "repository.json"
    with git_admin_lock(repository.common_dir) as handles:
        repository_id = _read_marker(marker, directory_fd=handles.directory_fd)
        if repository_id is None:
            repository_id = str(uuid.uuid4())
            payload = json.dumps(
                {"schema_version": 1, "repository_id": repository_id},
                sort_keys=True, separators=(",", ":"),
            ).encode() + b"\n"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(marker.name, flags, 0o600, dir_fd=handles.directory_fd)
            except FileExistsError:
                repository_id = _read_marker(marker, directory_fd=handles.directory_fd)
                if repository_id is None:
                    raise ContextRefused("REPOSITORY_IDENTITY_INVALID")
            else:
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(handles.directory_fd)
        store.ensure_context_schema()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        workspace_root = repository.primary_root.parent / ".ffs-workspaces" / repository_id
        with store.transaction() as tx:
            row = tx.execute(
                "SELECT common_dir, filesystem_id FROM context_repositories WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            if row is not None and (
                row["common_dir"] != os.fspath(repository.common_dir)
                or row["filesystem_id"] != repository.filesystem_id
            ):
                raise ContextRefused("REPOSITORY_REPLACED")
            if row is None:
                tx.execute(
                    "INSERT INTO context_repositories "
                    "(repository_id, marker_id, common_dir, filesystem_id, primary_root, workspace_root, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (repository_id, repository_id, os.fspath(repository.common_dir),
                     repository.filesystem_id, os.fspath(repository.primary_root),
                     os.fspath(workspace_root), now),
                )
        return repository_id


def resolve_context(
    request: ContextRequest, store, repository_id: str | None = None,
) -> RunContext:
    """Read one explicit registered context; never selects by timestamp."""
    if repository_id is None:
        repository_id = repository_identity(resolve_repository(request.cwd))
    inherited = request.inherited or os.environ
    run_id = select_run_id(request.explicit_run_id, inherited)
    with store.read_transaction() as tx:
        rows = tx.execute(
            "SELECT * FROM context_runs WHERE repository_id = ? AND run_id = ?",
            (repository_id, run_id),
        ).fetchall()
    if not rows:
        raise ContextRefused("RUN_NOT_FOUND")
    if len(rows) != 1:
        raise ContextRefused("AMBIGUOUS_RUN")
    row = rows[0]
    with store.read_transaction() as tx:
        activity = tx.execute(
            "SELECT runtime_tuple_hash,result_json FROM authority_activities WHERE id = ?",
            (row["activity_id"],),
        ).fetchone()
    workspace = Path(row["workspace"])
    state = row["state"]
    ready = False
    manifest_hash = _EMPTY_SELECTION_MANIFEST_HASH
    selected_input_count = 0
    if row["preparation_id"]:
        with store.read_transaction() as tx:
            prep = tx.execute(
                "SELECT selected_manifest_hash, selected_manifest_json, state, generation, created_by_ffs "
                "FROM context_workspaces WHERE preparation_id = ?",
                (row["preparation_id"],),
            ).fetchone()
        if prep is not None:
            manifest_hash = prep["selected_manifest_hash"]
            try:
                manifest_value = json.loads(prep["selected_manifest_json"])
                entries = manifest_value.get("entries") if isinstance(manifest_value, dict) else None
                if isinstance(entries, list):
                    selected_input_count = len(entries)
            except (TypeError, ValueError, json.JSONDecodeError):
                state = "blocked"
        if state == "ready":
            from run_state.workspace import inspect_ready_layout, inspect_workspace

            if (
                prep is None or prep["state"] != "ready"
                or prep["generation"] != row["generation"] or not prep["created_by_ffs"]
            ):
                state = "blocked"
            else:
                state = inspect_ready_layout(inspect_workspace(store, row["preparation_id"]))
            ready = state == "ready"
    elif state == "ready":
        state = "missing" if not workspace.is_dir() else "blocked"
    return RunContext(
        repository_id=repository_id,
        run_id=run_id,
        activity_id=row["activity_id"],
        workspace=row["workspace"],
        evidence_root=row["evidence_root"],
        generation=row["generation"],
        workspace_state=state,
        ready=ready,
        selected_input_manifest_hash=manifest_hash,
        selection_manifest_hash=manifest_hash,
        input_digest=row["input_digest"],
        selected_input_count=selected_input_count,
        result=(json.loads(activity["result_json"])
                if activity is not None and activity["result_json"] else None),
        upstream=json.loads(row["upstream_json"]),
    )


def select_activity(*args, **kwargs):
    """Compatibility boundary; activity selection is serialized by the CLI."""
    return kwargs.get("activity") or (args[0] if args else None)


def resolve_evidence(
    state_root: Path, run_id: str, repository_id: str | None = None,
) -> Path:
    """Return the repository-scoped fixture partition; two args preserve legacy projection."""
    run = validate_run_id(run_id)
    if repository_id is None:
        return Path(state_root) / "runs" / run
    try:
        repository = str(uuid.UUID(repository_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ContextRefused("REPOSITORY_IDENTITY_INVALID") from error
    return Path(state_root) / "runs" / repository / run
