"""Pinned, read-only bridge to the GSD planning resolver.

This module deliberately has no fallback to process environment or to a local
Node installation.  A caller must supply the controller-registered runtime
descriptor and the resolver runs only through :mod:`upstream_bridge.cjs`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_SCHEMA = "ffs.gsd-upstream-runtime/v1"
_VERSION = "1.14.0"
_RUNTIME_IDENTITY_SCHEMA = "ffs.gsd-upstream-runtime-identity/v1"
_EXECUTION_POLICY_SCHEMA = "ffs.gsd-upstream-execution-policy/v1"
_BRIDGE_NAME = "upstream_bridge.cjs"
_BRIDGE_SHA256 = "9310a47eedaf66ed6f547bd1090aa7e89046abb85fcf0128b98f9f3abc6d077c"
_ARGV_PREFIX = ("--jitless",)
_SESSION_ENVIRONMENT = ("GSD_SESSION_KEY",)
_UNSET_ENVIRONMENT = ("NODE_OPTIONS", "NODE_PATH")
_SHA256_LENGTH = 64
_MAX_OUTPUT_BYTES = 64 * 1024
_RESOLVE_TIMEOUT_SECONDS = 15.0
# Resolver scope segments are portable ASCII path components: the first byte
# is alphanumeric and subsequent bytes may also use dot, underscore, or dash.
_UPSTREAM_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUIRED_MODULES = frozenset(
    {
        "active-workstream-store.cjs",
        "cli-exit.cjs",
        "clock.cjs",
        "core-utils.cjs",
        "exit-code-registry.cjs",
        "frontmatter.cjs",
        "io.cjs",
        "markdown-sectionizer.cjs",
        "pattern.cjs",
        "phase-id.cjs",
        "plan-dependency-graph.cjs",
        "plan-scan.cjs",
        "planning-scope.cjs",
        "planning-workspace.cjs",
        "shell-command-projection.cjs",
        "text-lines.cjs",
        "unusable-input.cjs",
        "validate.cjs",
        "vendor/js-yaml.cjs",
        "vendor/re2js.cjs",
        "workstream-name-policy.cjs",
    }
)


class UpstreamRefused(RuntimeError):
    """The fixed upstream resolver cannot safely produce a binding."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _execution_policy() -> dict[str, Any]:
    """Return the fixed policy used by both runtime identity and launch."""
    return {
        "schema": _EXECUTION_POLICY_SCHEMA,
        "argv_prefix": list(_ARGV_PREFIX),
        "environment": {"OPENSSL_CONF": "", "PATH": os.defpath},
        "session_environment": list(_SESSION_ENVIRONMENT),
        "unset_environment": list(_UNSET_ENVIRONMENT),
        "bridge": {"path": _BRIDGE_NAME, "sha256": _BRIDGE_SHA256},
    }


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise UpstreamRefused("UPSTREAM_INVALID")
    # Check the original spelling before pathlib discards ``.`` and repeated
    # separators.  A descriptor must have one literal, canonical pathname.
    if not value.startswith("/") or value != os.path.normpath(value):
        raise UpstreamRefused("UPSTREAM_INVALID")
    path = Path(value)
    if not path.is_absolute():
        raise UpstreamRefused("UPSTREAM_INVALID")
    return path


def _relative_module_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise UpstreamRefused("UPSTREAM_INVALID")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UpstreamRefused("UPSTREAM_INVALID")
    if str(path) != value or "\\" in value:
        raise UpstreamRefused("UPSTREAM_INVALID")
    return value


def _validate_segment(value: object, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 160:
        raise UpstreamRefused("UPSTREAM_INVALID")
    if not _UPSTREAM_SEGMENT.fullmatch(value):
        raise UpstreamRefused("UPSTREAM_INVALID")
    return value


def _anchored_regular(path: Path) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    """Read one regular, single-link file through an absolute no-follow chain."""
    if not path.is_absolute():
        raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(path.anchor, directory_flags)
    parent_fd = root_fd
    file_fd = -1
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(path.name, flags, dir_fd=parent_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        identity = (
            before.st_dev,
            before.st_ino,
            stat.S_IMODE(before.st_mode),
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        observed = (
            after.st_dev,
            after.st_ino,
            stat.S_IMODE(after.st_mode),
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity != observed:
            raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
        return b"".join(chunks), identity
    except UpstreamRefused:
        raise
    except OSError as error:
        raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT") from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def _pinned_bridge_identity(
    path: Path,
    expected_sha256: str,
    *,
    previous: tuple[int, int, int, int, int, int] | None = None,
) -> tuple[int, int, int, int, int, int]:
    """Hash the anchored bridge and optionally require the same file identity."""
    data, identity = _anchored_regular(path)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
    if previous is not None and identity != previous:
        raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
    return identity


def _assert_anchored_directory(path: Path) -> None:
    if not path.is_absolute():
        raise UpstreamRefused("UPSTREAM_ESCAPE")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(path.anchor, flags)
    current_fd = root_fd
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
    except OSError as error:
        raise UpstreamRefused("UPSTREAM_ESCAPE") from error
    finally:
        os.close(current_fd)


def _assert_existing_planning_chain(
    workspace: Path,
    *,
    project: str | None,
    workstream: str | None,
) -> None:
    """Reject a symlink in every existing resolver-owned planning component.

    The resolver may legitimately select a leaf it has not created yet.  It
    may not traverse an existing symlink on the way to that leaf.
    """
    _assert_anchored_directory(workspace)
    current = workspace / ".planning"
    _assert_anchored_directory(current)
    parts: list[str] = []
    if project is not None:
        parts.append(project)
    if workstream is not None:
        parts.extend(("workstreams", workstream))
    for part in parts:
        candidate = current / part
        try:
            _assert_anchored_directory(candidate)
        except UpstreamRefused:
            # A missing selected leaf is a normal resolver result.  Anything
            # else (including a symlink or an unsafe ancestor) remains fatal.
            try:
                os.stat(candidate, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise
        current = candidate


def describe_runtime(module_root: Path, node_path: Path) -> dict[str, Any]:
    """Describe one closed private GSD module closure as a registrable descriptor.

    The controller registers this exact document (its SHA-256 is the run's
    immutable runtime binding); resume revalidates every byte through
    :meth:`UpstreamRuntime.verify` and refuses drift.  Nothing here reads the
    process environment or a shared profile.
    """
    # The descriptor binds the anchored real file, never a symlink alias.
    node = _absolute_path(os.fspath(Path(node_path).resolve()))
    root = _absolute_path(os.fspath(Path(module_root).resolve()))
    node_bytes, _ = _anchored_regular(node)
    _assert_anchored_directory(root)
    modules = {}
    for relative in sorted(_REQUIRED_MODULES):
        data, _ = _anchored_regular(root / relative)
        modules[relative] = hashlib.sha256(data).hexdigest()
    descriptor = {
        "schema": _SCHEMA, "version": _VERSION,
        "node": {"path": os.fspath(node), "sha256": hashlib.sha256(node_bytes).hexdigest()},
        "module_root": os.fspath(root), "modules": modules,
    }
    UpstreamRuntime.from_manifest(descriptor).verify()
    return descriptor


@dataclass(frozen=True)
class UpstreamRuntime:
    """A closed, controller-supplied Node/CJS resolver descriptor."""

    node_path: Path
    node_sha256: str
    module_root: Path
    modules: tuple[tuple[str, str], ...]
    runtime_digest: str

    @classmethod
    def from_manifest(cls, mapping: Mapping[str, Any]) -> "UpstreamRuntime":
        if not isinstance(mapping, Mapping) or set(mapping) != {
            "schema", "version", "node", "module_root", "modules",
        }:
            raise UpstreamRefused("UPSTREAM_INVALID")
        node = mapping["node"]
        modules = mapping["modules"]
        if (
            mapping["schema"] != _SCHEMA
            or mapping["version"] != _VERSION
            or not isinstance(node, Mapping)
            or set(node) != {"path", "sha256"}
            or not isinstance(modules, Mapping)
        ):
            raise UpstreamRefused("UPSTREAM_INVALID")
        node_path = _absolute_path(node["path"])
        module_root = _absolute_path(mapping["module_root"])
        if not _is_sha256(node["sha256"]):
            raise UpstreamRefused("UPSTREAM_INVALID")
        checked_modules: dict[str, str] = {}
        for relative, digest in modules.items():
            name = _relative_module_path(relative)
            if name in checked_modules or not _is_sha256(digest):
                raise UpstreamRefused("UPSTREAM_INVALID")
            checked_modules[name] = digest
        if set(checked_modules) != _REQUIRED_MODULES:
            raise UpstreamRefused("UPSTREAM_INVALID")
        canonical = {
            "schema": _SCHEMA,
            "version": _VERSION,
            "node": {"path": os.fspath(node_path), "sha256": node["sha256"]},
            "module_root": os.fspath(module_root),
            "modules": dict(sorted(checked_modules.items())),
        }
        identity = {
            "schema": _RUNTIME_IDENTITY_SCHEMA,
            "descriptor": canonical,
            "execution_policy": _execution_policy(),
        }
        return cls(
            node_path=node_path,
            node_sha256=node["sha256"],
            module_root=module_root,
            modules=tuple(sorted(checked_modules.items())),
            runtime_digest=hashlib.sha256(_canonical_json(identity)).hexdigest(),
        )

    def verify(self) -> None:
        node_bytes, _ = _anchored_regular(self.node_path)
        if hashlib.sha256(node_bytes).hexdigest() != self.node_sha256:
            raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
        _assert_anchored_directory(self.module_root)
        for relative, expected_hash in self.modules:
            data, _ = _anchored_regular(self.module_root / relative)
            if hashlib.sha256(data).hexdigest() != expected_hash:
                raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")


@dataclass(frozen=True)
class UpstreamBinding:
    project: str | None
    workstream: str | None
    session_key: str
    effective_session_key: str
    planning_root: str
    resolver_version: str
    runtime_digest: str

    def as_payload(self) -> dict[str, str | None]:
        return {
            "project": self.project,
            "workstream": self.workstream,
            "session_key": self.session_key,
            "effective_session_key": self.effective_session_key,
            "planning_root": self.planning_root,
            "resolver_version": self.resolver_version,
            "runtime_digest": self.runtime_digest,
        }


def _response_binding(value: object, runtime: UpstreamRuntime, workspace: Path) -> UpstreamBinding:
    if not isinstance(value, Mapping) or set(value) != {
        "project", "workstream", "session_key", "effective_session_key", "planning_root",
    }:
        raise UpstreamRefused("UPSTREAM_INVALID")
    project = _validate_segment(value["project"])
    workstream = _validate_segment(value["workstream"])
    session_key = _validate_segment(value["session_key"], allow_none=False)
    effective = _validate_segment(value["effective_session_key"], allow_none=False)
    planning_root = _absolute_path(value["planning_root"])
    expected = workspace / ".planning"
    if project is not None:
        expected /= project
    if workstream is not None:
        expected = expected / "workstreams" / workstream
    if planning_root != expected:
        raise UpstreamRefused("UPSTREAM_ESCAPE")
    _assert_existing_planning_chain(
        workspace,
        project=project,
        workstream=workstream,
    )
    return UpstreamBinding(
        project=project,
        workstream=workstream,
        session_key=session_key,
        effective_session_key=effective,
        planning_root=os.fspath(planning_root),
        resolver_version=_VERSION,
        runtime_digest=runtime.runtime_digest,
    )


def resolve_upstream_binding(
    workspace: Path,
    *,
    runtime: UpstreamRuntime,
    project: str | None = None,
    workstream: str | None = None,
    session_key: str | None = None,
    stored_workstream: str | None = None,
) -> UpstreamBinding:
    """Resolve GSD scope through the fixed bridge without ambient authority."""
    if not isinstance(runtime, UpstreamRuntime):
        raise UpstreamRefused("UPSTREAM_INVALID")
    workspace = _absolute_path(os.fspath(workspace))
    project = _validate_segment(project)
    workstream = _validate_segment(workstream)
    stored_workstream = _validate_segment(stored_workstream)
    session_key = _validate_segment(session_key, allow_none=False)
    _assert_anchored_directory(workspace)
    _assert_anchored_directory(workspace / ".planning")
    runtime.verify()
    policy = _execution_policy()
    bridge_policy = policy["bridge"]
    if not isinstance(bridge_policy, dict):  # fixed internal shape
        raise UpstreamRefused("UPSTREAM_RUNTIME_DRIFT")
    bridge = Path(__file__).with_name(str(bridge_policy["path"]))
    bridge_identity = _pinned_bridge_identity(bridge, bridge_policy["sha256"])
    request = {
        "workspace": os.fspath(workspace),
        "project": project,
        "workstream": workstream,
        "session_key": session_key,
        "stored_workstream": stored_workstream,
        "module_root": os.fspath(runtime.module_root),
    }
    try:
        completed = subprocess.run(
            [
                os.fspath(runtime.node_path),
                *policy["argv_prefix"],
                os.fspath(bridge),
            ],
            input=_canonical_json(request),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workspace,
            env={**policy["environment"], "GSD_SESSION_KEY": session_key},
            timeout=_RESOLVE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _pinned_bridge_identity(
            bridge, bridge_policy["sha256"], previous=bridge_identity,
        )
        raise UpstreamRefused("UPSTREAM_INVALID") from error
    _pinned_bridge_identity(
        bridge, bridge_policy["sha256"], previous=bridge_identity,
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > _MAX_OUTPUT_BYTES
        or len(completed.stderr) > _MAX_OUTPUT_BYTES
    ):
        raise UpstreamRefused("UPSTREAM_INVALID")
    try:
        response = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpstreamRefused("UPSTREAM_INVALID") from error
    runtime.verify()
    return _response_binding(response, runtime, workspace)
