"""Preparatory, fail-closed material for an artifact-only native review.

This module deliberately does *not* launch a native CLI, qualify a host, or
interpret a receipt.  It creates the private files and exact argv which a
Supervisor-owned, qualified adapter may later use.  In particular, a material
object is not evidence that the selected model actually ran. The evidence
verifier binds retained telemetry/session bytes to this material; it grants
no launch, receipt, or qualification authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, Final
import uuid

from host_capabilities import CapabilityError, codex_native_binary, codex_node_binary, codex_path_entries
from .claude_host import SUPPORTED_CLAUDE_VERSION

if TYPE_CHECKING:
    from .codex_host import CodexTelemetry
    from .claude_host import ClaudeTelemetry


NATIVE_REVIEW_SCHEMA: Final = "ffs.native-review-runtime/v1"
CODEX_RELEASE: Final = "rust-v0.154.0"
CODEX_COMMIT: Final = "6b9826e3aa83b1a5947db50f4332cb9c65f1b340"
CODEX_CLI_VERSION: Final = "0.154.0"
CLAUDE_CLI_VERSION: Final = SUPPORTED_CLAUDE_VERSION
CODEX_TOOL_SOURCE_SHA256: Final = "451622e76c45dd1585318c200fdee9a00d7aaf785d4a540facca1010146307b7"
CODEX_CONFIG_SCHEMA_SHA256: Final = "2e1fcf1cbb20f255c3baca2e174b4a3c954cef577a130587b8935e2d12c8ade6"
CODEX_MODELS_SOURCE_SHA256: Final = "2e9923d405a497441a0b264efc07de6ce21cdb108442e660a8b9fb63ca415aed"
# Audited snapshots, keyed by Codex CLI version.  Each row is the openai/codex tag commit
# plus the SHA-256 of the raw bytes of spec_plan.rs (tool registration), config.schema.json
# and openai_models.rs (model protocol) at that tag.  The constants above are the 0.154.0
# row, kept under their original names.  A version absent here is refused.
_CODEX_PINS: Final = {
    CODEX_CLI_VERSION: {
        "codex_release": CODEX_RELEASE, "codex_commit": CODEX_COMMIT,
        "tool_registration_sha256": CODEX_TOOL_SOURCE_SHA256,
        "config_schema_sha256": CODEX_CONFIG_SCHEMA_SHA256,
        "model_protocol_sha256": CODEX_MODELS_SOURCE_SHA256,
    },
    "0.159.0": {
        "codex_release": "rust-v0.159.0", "codex_commit": "687a119f0fcaace47e1f1abcc77cec6c813fd6da",
        "tool_registration_sha256": "849ef21d4e5c83febdc31eacd7609911d43e3f69a35168fe02ae899273b5ef3e",
        "config_schema_sha256": "eda7251b7e46e0b9d0f3d8eef5dab451e11a55d723e2b802e152b7208045836a",
        "model_protocol_sha256": "4c8b5cafd8c55db269f669e352321f787fcaf83785bce4476cb887abfce75dc6",
    },
    # Same tool-registration digest as 0.159.0; commit, config schema and model protocol differ.
    "0.160.0": {
        "codex_release": "rust-v0.160.0", "codex_commit": "a956835d020762cb2b570053af06f643a11c0ecc",
        "tool_registration_sha256": "849ef21d4e5c83febdc31eacd7609911d43e3f69a35168fe02ae899273b5ef3e",
        "config_schema_sha256": "7ce31bde1ed6ef15c53a96ba460bb1d0fb7b99fd9ab719b567c94c474f62b023",
        "model_protocol_sha256": "961f3051af96a988f37151b9a3c1e92103d6d76a81a01a1a2fb8a30328e4282d",
    },
}
# Features whose key exists only in some audited schemas.  --strict-config rejects an unknown
# feature key, so these are passed for exactly the listed versions.  From 0.156.1 spec_plan.rs
# registers send_message_to_user_async when that (default-off) feature is enabled.
_CODEX_EXTRA_DISABLED: Final = {"0.159.0": ("send_message_to_user_async",),
                                "0.160.0": ("send_message_to_user_async",)}
# Provenance rows naming the executables a `.js` Codex launcher runs: the Node it is started
# under and the vendor native executable it spawns (F53).  (path row, digest row, label)
_BOUND_BINARIES: Final = (("node_binary", "node_sha256", "Node binary"),
                          ("native_binary", "native_sha256", "vendor Codex binary"))
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MODEL = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_VERSION = re.compile(r"[A-Za-z0-9._+-]{1,128}\Z")
_MAX_FILE = 2 * 1024 * 1024
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "view_image", "multi_agent", "multi_agent_v2",
    "plugins", "remote_plugin", "recommended_plugins", "plugin_sharing", "apps",
    "code_mode_host", "code_mode", "code_mode_only", "image_generation",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access", "computer_use",
    "hooks", "goals", "sleep_tool", "skill_search", "skill_mcp_dependency_install",
    "workspace_dependencies", "deferred_executor", "deferred_tool_world_state",
    "request_permissions_tool", "token_budget", "current_time_reminder", "tool_suggest",
    "standalone_web_search", "enable_mcp_apps", "context_management",
)


class NativeReviewRuntimeRefused(ValueError):
    """The staged review closure is absent, unsafe, or no longer identical."""

    code = "NATIVE_REVIEW_MATERIAL_INVALID"


class NativeReviewCatalogUnavailable(NativeReviewRuntimeRefused):
    """The qualified Codex binary did not produce its bundled model catalog."""

    code = "NATIVE_REVIEW_CATALOG_UNAVAILABLE"


class NativeReviewModelUnavailable(NativeReviewRuntimeRefused):
    """The requested model is absent from the catalog; no other slug is substituted."""

    code = "NATIVE_REVIEW_MODEL_UNAVAILABLE"


_BUNDLED_CATALOG_TIMEOUT = 30


def _pinned_provenance(host: str, version: str) -> dict[str, str]:
    """Provenance row for the exact CLI version, or refuse.  Claude material keeps the
    original 0.154.0 rows (it always did); only its own CLI version is checked."""
    if (host == "claude" and version == CLAUDE_CLI_VERSION) or (host == "codex" and version in _CODEX_PINS):
        pin = _CODEX_PINS[CODEX_CLI_VERSION if host == "claude" else version]
        return {**pin, "preparation_only": "qualification-and-receipts-required"}
    raise NativeReviewRuntimeRefused("CLI version is not the pinned native review version")


def _disabled_features(version: str) -> tuple[str, ...]:
    return (*_DISABLED_FEATURES, *_CODEX_EXTRA_DISABLED.get(version, ()))


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise NativeReviewRuntimeRefused(f"{label} is not a SHA-256 digest")
    return value


def _text(value: object, label: str, pattern: re.Pattern[str], *, limit: int = 128) -> str:
    if not isinstance(value, str) or "\0" in value or len(value) > limit or pattern.fullmatch(value) is None:
        raise NativeReviewRuntimeRefused(f"{label} is malformed")
    return value


def _private_directory(path: Path, label: str) -> tuple[Path, tuple[int, int]]:
    try:
        info = path.lstat()
    except OSError as error:
        raise NativeReviewRuntimeRefused(f"{label} is unavailable") from error
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise NativeReviewRuntimeRefused(f"{label} is unsafe")
    return path.resolve(strict=True), (info.st_dev, info.st_ino)


def _review_workspace(path: Path) -> tuple[Path, tuple[int, int]]:
    """A review cwd is read-only at launch, but must not be a link alias."""
    try:
        info = path.lstat()
    except OSError as error:
        raise NativeReviewRuntimeRefused("review workspace is unavailable") from error
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise NativeReviewRuntimeRefused("review workspace is unsafe")
    return path.resolve(strict=True), (info.st_dev, info.st_ino)


def _read_regular(path: Path, label: str, *, private: bool = False) -> tuple[bytes, tuple[int, int]]:
    return _read_checked(path, label, private=private, binary=False)


def _read_checked(path: Path, label: str, *, private=False, binary=False):
    """Walk pinned directory descriptors and bound data reads before allocation."""
    descriptors = []
    try:
        if not path.is_absolute() or '..' in path.parts:
            raise NativeReviewRuntimeRefused(f"{label} is unsafe")
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(directory)
        for component in path.parts[1:-1]:
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in ({0, os.getuid()} if binary else {os.getuid()})
                or info.st_nlink != 1 or info.st_mode & 0o022
                or (private and stat.S_IMODE(info.st_mode) != 0o600)
                or (binary and not info.st_mode & 0o111)
                or info.st_size > (512 * 1024 * 1024 if binary else _MAX_FILE)):
            raise NativeReviewRuntimeRefused(f"{label} is unsafe")
        digest, chunks, count = hashlib.sha256(), [], 0
        while chunk := os.read(descriptor, 64 * 1024):
            count += len(chunk)
            if count > info.st_size:
                raise NativeReviewRuntimeRefused(f"{label} changed while reading")
            digest.update(chunk)
            if not binary:
                chunks.append(chunk)
        current = os.fstat(descriptor)
        linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                or (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino) or count != info.st_size
                or path.resolve(strict=True) != path):
            raise NativeReviewRuntimeRefused(f"{label} changed while reading")
        return (digest.hexdigest() if binary else b''.join(chunks)), (info.st_dev, info.st_ino)
    except OSError as error:
        raise NativeReviewRuntimeRefused(f"{label} is unsafe or unavailable") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_private_new(path: Path, data: bytes) -> tuple[str, tuple[int, int]]:
    descriptors = []
    try:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(directory)
        for component in path.parts[1:-1]:
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise NativeReviewRuntimeRefused("private review parent is unsafe")
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
    except OSError as error:
        raise NativeReviewRuntimeRefused("cannot publish private review artifact") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    read, identity = _read_regular(path, "private review artifact", private=True)
    if read != data:
        raise NativeReviewRuntimeRefused("private review artifact changed during publication")
    return _digest(read), identity


def _catalog_model(source: object, requested_model: str) -> tuple[dict[str, object], str]:
    if not isinstance(source, dict):
        raise NativeReviewRuntimeRefused("model catalog is not an object")
    models = source.get("models")
    if not isinstance(models, list) or not models:
        raise NativeReviewRuntimeRefused("model catalog has no models")
    matches = [item for item in models if isinstance(item, dict) and item.get("slug") == requested_model]
    if not matches:
        raise NativeReviewModelUnavailable("requested model is absent from the catalog")
    if len(matches) != 1:
        raise NativeReviewRuntimeRefused("requested model is not uniquely present in catalog")
    # Preserve original model metadata, only removing known tool-registration
    # paths.  StaticModelsManager reads this response; it is not a replacement
    # for the user's active catalog.
    model = json.loads(_canonical(matches[0]).decode("utf-8"))
    model["apply_patch_tool_type"] = None
    model["experimental_supported_tools"] = []
    # A catalog-selected code_mode_only survives feature disables and produces
    # a runtime error when its host is disabled. Direct mode plus the closed
    # registry exposes no executable model tools, without relying on failure.
    model['tool_mode'] = 'direct'
    # Keep response metadata (for example rollout/auth metadata understood by
    # the installed parser), but never retain any second model candidate.
    catalog = {key: item for key, item in source.items() if key not in {"models", "default_model"}}
    catalog.update({"models": [model], "default_model": requested_model})
    return catalog, _digest(_canonical(catalog))


def _leader_exited(pid: int, deadline: float) -> bool:
    """Wait for the leader's exit before the deadline without reaping it (its pid keeps the group id)."""
    while True:
        try:
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:
            return False  # already reaped elsewhere (for example SIGCHLD ignored): status unverifiable
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _bundled_catalog(binary: Path, path: str, parent: Path) -> bytes:
    """The catalog shipped inside the qualified binary (``debug models --bundled``: offline, no refresh).

    It runs from ``/`` with only the closed review environment and a fresh empty 0700 home below the
    private parent (no credential, so nothing can authenticate), and the home is removed afterwards.
    Output is accepted only at end of file, when every writer (descendants included) has closed it,
    and once the leader has exited, within one deadline and at most _MAX_FILE bytes.  The group is
    killed before the leader is reaped, while the leader's pid still reserves the group id.
    """
    try:
        home = Path(tempfile.mkdtemp(prefix=".bundled-catalog-", dir=parent))
    except OSError as error:
        raise NativeReviewCatalogUnavailable("cannot create the bundled catalog home") from error
    try:
        try:
            process = subprocess.Popen(
                (str(binary), "debug", "models", "--bundled"), cwd="/",
                env=dict(_environment("codex", home, Path("/"), path)), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            raise NativeReviewCatalogUnavailable("cannot start the bundled catalog") from error
        output, complete = bytearray(), False
        try:
            deadline = time.monotonic() + _BUNDLED_CATALOG_TIMEOUT
            descriptor = process.stdout.fileno()
            while len(output) <= _MAX_FILE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if not select.select([descriptor], [], [], remaining)[0]:
                    continue
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    complete = _leader_exited(process.pid, deadline)
                    break
                output += chunk
        except OSError as error:
            raise NativeReviewCatalogUnavailable("bundled catalog output is unreadable") from error
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.stdout.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if not complete or process.returncode != 0 or not output or len(output) > _MAX_FILE:
        raise NativeReviewCatalogUnavailable("bundled catalog was not produced")
    return bytes(output)


def _codex_overrides(catalog: Path, version: str) -> tuple[str, ...]:
    return (f'model_catalog_json={json.dumps(str(catalog))}', 'web_search="disabled"',
            'tools.experimental_request_user_input.enabled=false', 'tools.update_plan.enabled=false',
            'mcp_servers={}', *(f'features.{feature}=false' for feature in _disabled_features(version)))


def _codex_config(catalog: Path, version: str) -> bytes:
    # Tool registration in pinned spec_plan.rs has independent routes: shell
    # registration, apply_patch_tool_type, experimental catalog tools, the
    # request-input/plan gates and, in versions whose audited schema has the key,
    # the send_message_to_user_async feature gate.  All are closed here for every
    # admitted version, rather than treating a quiet transcript as proof of tool absence.
    rendered = "\n".join((
        f'model_catalog_json = {json.dumps(str(catalog))}',
        'web_search = "disabled"',
        '', '[features]',
        *(f'{feature} = false' for feature in _disabled_features(version)),
        '', '[tools.experimental_request_user_input]', 'enabled = false',
        '', '[tools.update_plan]', 'enabled = false', '',
    ))
    return rendered.encode("utf-8")


def _claude_mcp() -> bytes:
    return b'{"mcpServers":{}}\n'


def _review_path(host: str, binary: Path, node: Path | None) -> str:
    """PATH of the review process.  Codex gets what its host launch gets (the verified Node's
    directory first for a `.js` launcher, then the launcher's, then the system dirs); Claude
    keeps the system dirs."""
    if host != "codex":
        return "/usr/bin:/bin"
    entries = codex_path_entries(binary, node)
    if any("\0" in entry or os.pathsep in entry or not os.path.isabs(entry) for entry in entries):
        raise NativeReviewRuntimeRefused("native review PATH is unsafe")
    return os.pathsep.join(entries)


def _environment(host, root, workspace, path):
    return tuple(sorted({"HOME": str(root),
                         "CODEX_HOME" if host == "codex" else "CLAUDE_CONFIG_DIR":
                             str(root if host == "codex" else root / "claude-config"),
                         "PWD": str(workspace), "PATH": path, "LANG": "C.UTF-8",
                         "LC_ALL": "C.UTF-8", "NO_COLOR": "1"}.items()))


def _bind_binary(path: Path, pin: str, label: str) -> tuple[str, str]:
    """One read of the file whose bytes are bound, compared with the qualified pin."""
    digest, _identity = _read_checked(path, label, binary=True)
    if digest != pin:
        raise NativeReviewRuntimeRefused(f"{label} differs from the qualified chain")
    return str(path), digest


def _bound_binaries(host: str, binary: str, provenance: object) -> dict[str, str]:
    """Executables a stored Codex material binds, each re-hashed from its stored path.

    A `.js` launcher must carry both its Node and its vendor executable.  Replay never
    consults the ambient ``CODEX_NODE_BINARY``/``CODEX_NATIVE_BINARY``/PATH: the preparing
    process resolved and pinned them once, and any other process must find the same bytes.
    """
    try:
        given = dict(provenance)
    except (TypeError, ValueError):
        raise NativeReviewRuntimeRefused("native review provenance drifted") from None
    if host != "codex":
        return {}  # a stray row on a Claude material fails the provenance comparison
    js = Path(binary).suffix == ".js"
    bound: dict[str, str] = {}
    for path_key, sha_key, label in _BOUND_BINARIES:
        row = {key: given[key] for key in (path_key, sha_key) if key in given}
        if not row and not js:
            continue
        if set(row) != {path_key, sha_key} or not all(isinstance(item, str) for item in row.values()):
            raise NativeReviewRuntimeRefused(f"Codex JS launcher material lacks its {label} identity")
        if path_key == "node_binary" and Path(row[path_key]).name != "node":
            raise NativeReviewRuntimeRefused("Node binary drifted: `node` must be the regular file itself")
        digest, _identity = _read_checked(Path(row[path_key]), label, binary=True)
        if digest != _sha(row[sha_key], label):
            raise NativeReviewRuntimeRefused(f"{label} drifted")
        bound.update(row)
    return bound


def _validate_session_id(host, session_id):
    if host == "codex":
        if session_id is not None:
            raise NativeReviewRuntimeRefused("Codex review cannot request a Claude session")
        return
    try:
        if not isinstance(session_id, str) or str(uuid.UUID(session_id)) != session_id:
            raise ValueError
    except ValueError as error:
        raise NativeReviewRuntimeRefused("Claude review requires a canonical session UUID") from error


def _argv(host, binary, model, effort, workspace, catalog, mcp, session_id, version):
    _validate_session_id(host, session_id)
    if not (host == "claude" and effort is None) and (
            not isinstance(effort, str) or effort not in {"low", "medium", "high", "xhigh", "max"}):
        raise NativeReviewRuntimeRefused("requested effort is malformed")
    if host == "codex":
        return (binary, "exec", "--json", "--strict-config", "--ignore-user-config", "--ignore-rules",
                "--sandbox", "read-only", "--cd", str(workspace), "--color", "never", "--model", model,
                "-c", f'model_reasoning_effort="{effort}"',
                *(part for value in _codex_overrides(Path(catalog), version) for part in ("-c", value)))
    effort_args = () if effort is None else ("--effort", effort)
    return (binary, "--model", model, *effort_args, "--tools", "", "--disable-slash-commands",
            "--strict-mcp-config", "--mcp-config", str(mcp), "--setting-sources", "",
            "--output-format", "stream-json", "--verbose", "--session-id", session_id, "-p")


@dataclass(frozen=True)
class NativeReviewRequest:
    """Caller-resolved identities; no ambient model/default resolution occurs."""

    host: str
    requested_model: str
    cli_version: str
    binary: str
    binary_sha256: str
    runtime_identity: str
    prompt: str
    effort: str | None = "high"
    catalog_path: str | None = None
    catalog_sha256: str | None = None
    session_id: str | None = None
    # The Node and the vendor executable the reviewer runtime was qualified with (the
    # qualified chain's ``node_sha256`` / ``native_sha256``); a `.js` launcher requires both.
    node_sha256: str | None = None
    native_sha256: str | None = None


@dataclass(frozen=True)
class NativeReviewMaterial:
    """Closed pre-launch material, not a qualification or execution receipt."""

    schema: str
    host: str
    requested_model: str
    cli_version: str
    binary: str
    binary_sha256: str
    runtime_identity: str
    runtime_root: str
    runtime_device: int
    runtime_inode: int
    workspace: str
    workspace_device: int
    workspace_inode: int
    config_path: str | None
    config_sha256: str | None
    catalog_path: str | None
    catalog_sha256: str | None
    source_catalog_sha256: str | None
    mcp_path: str | None
    mcp_sha256: str | None
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    prompt_sha256: str
    provenance: tuple[tuple[str, str], ...]
    effort: str | None = "high"
    session_id: str | None = None
    claude_config_device: int | None = None
    claude_config_inode: int | None = None

    def replay_binding(self) -> dict[str, object]:
        """Non-secret binding suitable for future qualification and receipts."""
        return {
            "schema": self.schema, "operation": "native-artifact-review-preparation",
            "host": self.host, "requested_model": self.requested_model,
            "cli_version": self.cli_version, "binary_sha256": self.binary_sha256,
            "effort": self.effort,
            "session_id": self.session_id,
            "claude_config_device": self.claude_config_device,
            "claude_config_inode": self.claude_config_inode,
            "runtime_identity": self.runtime_identity, "config_sha256": self.config_sha256,
            "catalog_sha256": self.catalog_sha256, "source_catalog_sha256": self.source_catalog_sha256,
            "mcp_sha256": self.mcp_sha256, "prompt_sha256": self.prompt_sha256,
            "argv_sha256": _digest(_canonical(self.argv)),
            "environment_sha256": _digest(_canonical(self.environment)),
            "provenance": dict(self.provenance),
        }


def prepare_native_review_runtime(request: NativeReviewRequest, *, runtime_root: Path,
                                  workspace: Path) -> NativeReviewMaterial:
    """Stage one fresh private runtime and return exact future native argv.

    The caller must already have selected and bound prompt/artifacts in the
    artifact envelope.  This function never reads credentials.  The processes it starts are the
    verified Node's platform probe and, for Codex without a fixture catalog, the verified binary's
    offline ``debug models --bundled``.  ``runtime_root`` must be a new leaf below a private directory.
    """
    if type(request) is not NativeReviewRequest:
        raise NativeReviewRuntimeRefused("review request has invalid type")
    if request.host not in {"codex", "claude"}:
        raise NativeReviewRuntimeRefused("review host is unsupported")
    _validate_session_id(request.host, request.session_id)
    model = _text(request.requested_model, "requested model", _MODEL)
    version = _text(request.cli_version, "CLI version", _VERSION)
    provenance = _pinned_provenance(request.host, version)
    binary = Path(request.binary)
    if not binary.is_absolute():
        raise NativeReviewRuntimeRefused("CLI binary must be absolute")
    binary_digest, _binary_identity = _read_checked(binary, "CLI binary", binary=True)
    if binary_digest != _sha(request.binary_sha256, "CLI binary"):
        raise NativeReviewRuntimeRefused("CLI binary differs from caller-resolved identity")
    node = None
    if request.host == "codex":
        js = binary.suffix == ".js"
        if js and request.node_sha256 is None:
            raise NativeReviewRuntimeRefused("Codex JS launcher requires its qualified Node pin")
        if js and request.native_sha256 is None:
            raise NativeReviewRuntimeRefused("Codex JS launcher requires its qualified native pin")
        for pin in (request.node_sha256, request.native_sha256):
            if pin is not None:
                _sha(pin, "qualified chain pin")
        try:
            node = codex_node_binary(binary, request.node_sha256 is not None)
            if node is not None:
                # The platform probe below runs this Node, so its bytes meet the pin first.
                provenance["node_binary"], provenance["node_sha256"] = _bind_binary(
                    node, request.node_sha256, _BOUND_BINARIES[0][2])
            native = codex_native_binary(binary, node)
        except CapabilityError as error:
            raise NativeReviewRuntimeRefused(str(error)) from error
        if native is not None:
            provenance["native_binary"], provenance["native_sha256"] = _bind_binary(
                native, request.native_sha256, _BOUND_BINARIES[1][2])
    elif request.node_sha256 is not None or request.native_sha256 is not None:
        raise NativeReviewRuntimeRefused("Claude review cannot bind a Node or vendor binary")
    path = _review_path(request.host, binary, node)
    if (not isinstance(request.runtime_identity, str) or not request.runtime_identity
            or "\0" in request.runtime_identity or len(request.runtime_identity) > 512):
        raise NativeReviewRuntimeRefused("runtime identity is malformed")
    if (not isinstance(request.prompt, str) or not request.prompt or "\0" in request.prompt
            or len(request.prompt.encode("utf-8")) > 64 * 1024):
        raise NativeReviewRuntimeRefused("bound review prompt is malformed")
    workspace, workspace_identity = _review_workspace(Path(workspace))
    root = Path(runtime_root)
    if not root.is_absolute() or root.name in {"", ".", ".."} or root.exists() or root.is_symlink():
        raise NativeReviewRuntimeRefused("private runtime root must be a new absolute leaf")
    parent, _parent_identity = _private_directory(root.parent, "private runtime parent")
    if root.parent.resolve(strict=True) != parent:
        raise NativeReviewRuntimeRefused("private runtime parent changed")
    catalog = catalog_digest = source_digest = None
    if request.host == "codex":
        catalog, catalog_digest, source_digest = _review_catalog(request, model, binary, path, parent)
        # The bytes that produced the catalog are the pinned ones: re-hash the chain after it ran.
        if _read_checked(binary, "CLI binary", binary=True)[0] != request.binary_sha256:
            raise NativeReviewRuntimeRefused("CLI binary changed while its catalog ran")
        for name, pin, label in _BOUND_BINARIES:
            if name in provenance:
                _bind_binary(Path(provenance[name]), provenance[pin], label)
    try:
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
    except OSError as error:
        raise NativeReviewRuntimeRefused("cannot create private runtime root") from error
    root, root_identity = _private_directory(root, "private runtime root")
    environment = _environment(request.host, root, workspace, path)
    config_path = catalog_path = mcp_path = None
    config_digest = mcp_digest = None
    claude_config_identity = (None, None)
    if request.host == "codex":
        catalog_path = root / "review-model-catalog.json"
        written_catalog, _catalog_identity = _write_private_new(catalog_path, _canonical(catalog))
        if written_catalog != catalog_digest:
            raise NativeReviewRuntimeRefused("private review catalog changed during publication")
        config_path = root / "config.toml"
        config_digest, _config_identity = _write_private_new(config_path, _codex_config(catalog_path, version))
    else:
        # Claude's empty private MCP plus these flags disable built-ins/slash
        # commands and all settings sources while retaining subscription OAuth;
        # --bare would incorrectly remove that authentication route.
        # The existing credential guard requires a private config child of HOME.
        profile = root / "claude-config"
        try:
            profile.mkdir(mode=0o700)
        except OSError as error:
            raise NativeReviewRuntimeRefused("cannot create private Claude config") from error
        profile, claude_config_identity = _private_directory(profile, "Claude config")
        mcp_path = profile / "empty-mcp.json"
        mcp_digest, _mcp_identity = _write_private_new(mcp_path, _claude_mcp())
    argv = (*_argv(request.host, str(binary.resolve()), model, request.effort, workspace,
                   catalog_path, mcp_path, request.session_id, version), request.prompt)
    return NativeReviewMaterial(
        NATIVE_REVIEW_SCHEMA, request.host, model, version, str(binary.resolve()), request.binary_sha256,
        request.runtime_identity, str(root), root_identity[0], root_identity[1], str(workspace),
        workspace_identity[0], workspace_identity[1], str(config_path) if config_path else None, config_digest,
        str(catalog_path) if catalog_path else None, catalog_digest, source_digest,
        str(mcp_path) if mcp_path else None, mcp_digest, argv, environment, _digest(request.prompt.encode("utf-8")),
        tuple(sorted(provenance.items())), request.effort, request.session_id, *claude_config_identity,
    )


def _review_catalog(request: NativeReviewRequest, model: str, binary: Path, path: str,
                    parent: Path) -> tuple[dict[str, object], str, str]:
    """The one-model catalog, its digest and its source digest.  Production passes no catalog, so the source is
    the qualified binary's bundled catalog; an explicit file is the unit-fixture route only."""
    if request.catalog_path is None and request.catalog_sha256 is None:
        source_data = _bundled_catalog(binary, path, parent)
    elif request.catalog_path is None or request.catalog_sha256 is None:
        raise NativeReviewRuntimeRefused("model catalog path and identity must be given together")
    else:
        source_path = Path(request.catalog_path)
        if not source_path.is_absolute():
            raise NativeReviewRuntimeRefused("model catalog must be absolute")
        source_data, _source_identity = _read_regular(source_path, "model catalog")
        if _digest(source_data) != _sha(request.catalog_sha256, "model catalog"):
            raise NativeReviewRuntimeRefused("model catalog differs from caller-resolved identity")
    try:
        source = json.loads(source_data.decode("utf-8"))
        return (*_catalog_model(source, model), _digest(source_data))
    except NativeReviewModelUnavailable:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, NativeReviewRuntimeRefused) as error:
        # A bundled catalog the binary emitted but FFS cannot use is an unavailable catalog.
        if request.catalog_path is None:
            raise NativeReviewCatalogUnavailable("bundled catalog is malformed") from error
        if isinstance(error, NativeReviewRuntimeRefused):
            raise
        raise NativeReviewRuntimeRefused("model catalog is not valid JSON") from error


def validate_native_review_material(value: object) -> NativeReviewMaterial:
    """Refuse tampered, replayed, retargeted, or unqualified material."""
    if type(value) is not NativeReviewMaterial or value.schema != NATIVE_REVIEW_SCHEMA:
        raise NativeReviewRuntimeRefused("native review material has invalid type")
    root, root_identity = _private_directory(Path(value.runtime_root), "private runtime root")
    if root_identity != (value.runtime_device, value.runtime_inode):
        raise NativeReviewRuntimeRefused("private runtime root was replaced")
    workspace, workspace_identity = _review_workspace(Path(value.workspace))
    if workspace_identity != (value.workspace_device, value.workspace_inode):
        raise NativeReviewRuntimeRefused("review workspace was replaced")
    binary_digest, _binary_identity = _read_checked(Path(value.binary), "CLI binary", binary=True)
    if binary_digest != _sha(value.binary_sha256, "CLI binary"):
        raise NativeReviewRuntimeRefused("CLI binary drifted")
    model = _text(value.requested_model, "requested model", _MODEL)
    version = _text(value.cli_version, "CLI version", _VERSION)
    expected_provenance = _pinned_provenance(value.host, version)
    bound = _bound_binaries(value.host, value.binary, value.provenance)
    expected_provenance.update(bound)
    expected_path = _review_path(value.host, Path(value.binary),
                                 Path(bound["node_binary"]) if "node_binary" in bound else None)
    if value.environment != _environment(value.host, root, workspace, expected_path):
        raise NativeReviewRuntimeRefused("native review environment is not closed")
    if value.host == "codex":
        if value.claude_config_device is not None or value.claude_config_inode is not None:
            raise NativeReviewRuntimeRefused("Codex material cannot bind a Claude config")
        if not all((value.config_path, value.config_sha256, value.catalog_path, value.catalog_sha256,
                    value.source_catalog_sha256)) or value.mcp_path is not None:
            raise NativeReviewRuntimeRefused("Codex material closure is incomplete")
        if Path(value.config_path).parent != root or Path(value.catalog_path).parent != root:
            raise NativeReviewRuntimeRefused("Codex review closure escaped private runtime")
        config_data, _config_identity = _read_regular(Path(value.config_path), "review config", private=True)
        catalog_data, _catalog_identity = _read_regular(Path(value.catalog_path), "review catalog", private=True)
        if _digest(config_data) != value.config_sha256 or _digest(catalog_data) != value.catalog_sha256:
            raise NativeReviewRuntimeRefused("private review closure drifted")
        try:
            catalog = json.loads(catalog_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise NativeReviewRuntimeRefused("private review catalog is malformed") from error
        rebuilt, expected_catalog = _catalog_model(catalog, model)
        if expected_catalog != value.catalog_sha256 or rebuilt != catalog:
            raise NativeReviewRuntimeRefused("private review catalog permits unapproved tools")
        expected_config = _codex_config(Path(value.catalog_path), version)
        if config_data != expected_config:
            raise NativeReviewRuntimeRefused("private review config differs from tool restriction proof")
    elif value.host == "claude":
        profile, profile_identity = _private_directory(root / "claude-config", "Claude config")
        if (type(value.claude_config_device) is not int or type(value.claude_config_inode) is not int
                or profile_identity != (value.claude_config_device, value.claude_config_inode)):
            raise NativeReviewRuntimeRefused("Claude config was replaced")
        if not value.mcp_path or not value.mcp_sha256 or any((value.config_path, value.catalog_path,
                                                               value.config_sha256, value.catalog_sha256)):
            raise NativeReviewRuntimeRefused("Claude material closure is incomplete")
        if Path(value.mcp_path).parent != profile:
            raise NativeReviewRuntimeRefused("Claude review closure escaped private runtime")
        mcp_data, _mcp_identity = _read_regular(Path(value.mcp_path), "private MCP config", private=True)
        if _digest(mcp_data) != value.mcp_sha256 or mcp_data != _claude_mcp():
            raise NativeReviewRuntimeRefused("private MCP closure drifted")
    else:
        raise NativeReviewRuntimeRefused("review host is unsupported")
    expected_argv = _argv(value.host, value.binary, model, value.effort, workspace,
                          value.catalog_path, value.mcp_path, value.session_id, version)
    if value.provenance != tuple(sorted(expected_provenance.items())):
        raise NativeReviewRuntimeRefused("native review provenance drifted")
    if (not isinstance(value.argv, tuple) or not value.argv or tuple(value.argv[:-1]) != expected_argv
            or not isinstance(value.argv[-1], str) or not value.argv[-1]):
        raise NativeReviewRuntimeRefused("native review argv was retargeted")
    if _digest(value.argv[-1].encode("utf-8")) != value.prompt_sha256:
        raise NativeReviewRuntimeRefused("bound review prompt drifted")
    return value


@dataclass(frozen=True)
class CodexReviewObservation:
    """Evidence binding only; never a qualified runtime or authority receipt."""

    material_sha256: str
    telemetry: CodexTelemetry
    session_sha256: str
    turn_id: str
    effective_model: str
    effective_effort: str


def verify_codex_review_evidence(material: object, stream: bytes, *,
                                 exit_code: int) -> CodexReviewObservation:
    """Join closed review material to terminal telemetry and its retained session.

    The future Supervisor transport must supply its captured stream and exit
    code. This read-only verifier does not prove their process provenance,
    authentication, prompt delivery, or restricted-profile qualification, and
    cannot admit a runtime or issue a review receipt. Scripted evidence can
    exercise this boundary without running any native CLI or model.
    """
    from .codex_host import (
        CodexHostRefused, parse_codex_telemetry, verify_artifact_review_session,
    )

    value = validate_native_review_material(material)
    if value.host != "codex":
        raise NativeReviewRuntimeRefused("Codex review material is required")
    if type(exit_code) is not int or exit_code != 0:
        raise NativeReviewRuntimeRefused("Codex review did not exit successfully")
    try:
        telemetry = parse_codex_telemetry(stream)
        session = verify_artifact_review_session(
            Path(value.runtime_root), thread_id=telemetry.thread_id,
            workspace=Path(value.workspace), model=value.requested_model, effort=value.effort,
        )
    except CodexHostRefused as error:
        raise NativeReviewRuntimeRefused(str(error)) from error
    validate_native_review_material(value)
    # The full material also binds runtime/workspace locators and inode identity,
    # which the earlier preparatory replay_binding intentionally did not carry.
    return CodexReviewObservation(
        _digest(_canonical(asdict(value))), telemetry, session["session_sha256"],
        session["turn_id"], session["effective_model"], session["effective_effort"],
    )


@dataclass(frozen=True)
class ClaudeReviewObservation:
    """Evidence only; Claude telemetry does not prove effective effort."""

    material_sha256: str
    telemetry: ClaudeTelemetry


def verify_claude_review_evidence(material: object, stream: bytes, *,
                                  exit_code: int) -> ClaudeReviewObservation:
    """Bind a caller-selected session to strict, tool-free review telemetry.

    As with the Codex observation, process provenance, authentication, prompt
    delivery and profile qualification remain the future transport's duties.
    No native execution, runtime admission or authority receipt occurs here.
    """
    from .claude_host import ClaudeHostRefused, parse_claude_telemetry

    value = validate_native_review_material(material)
    if value.host != "claude":
        raise NativeReviewRuntimeRefused("Claude review material is required")
    if type(exit_code) is not int or exit_code != 0:
        raise NativeReviewRuntimeRefused("Claude review did not exit successfully")
    try:
        telemetry = parse_claude_telemetry(
            stream, requested_model=value.requested_model, expected_session_id=value.session_id,
            expected_version=value.cli_version, artifact_review_workspace=value.workspace,
        )
    except ClaudeHostRefused as error:
        raise NativeReviewRuntimeRefused(str(error)) from error
    validate_native_review_material(value)
    return ClaudeReviewObservation(_digest(_canonical(asdict(value))), telemetry)
