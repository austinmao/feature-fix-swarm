"""Closed Codex ``exec`` transport for an already-qualified private runtime.

The adapter deliberately has no argv-taking entry point.  A caller supplies a
prompt and an admitted ``QualifiedCodexRuntime``; this module rebuilds the one
permitted command and treats incomplete or malformed CLI telemetry as an
uncertain result.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Final

from host_capabilities import (
    CapabilityError,
    DISABLED_NATIVE_FEATURES,
    GsdSupervisorEnvironment,
    QualifiedCodexRuntime,
    _binary_chain,
    _canonical_git_worktree,
    _native_jsonl,
    codex_closed_environment,
    codex_environment_policy_hash,
    codex_private_tmp_root,
    gsd_supervisor_environment_from_process,
    validate_gsd_supervisor_environment,
)
from process_identity import DEAD, ProcessIdentity, probe_identity


_MAX_TELEMETRY_BYTES: Final = 2 * 1024 * 1024
_MAX_INTEGER: Final = 2**63 - 1
_TOKEN_FIELDS: Final = frozenset((
    "input_tokens", "cached_input_tokens", "output_tokens",
    "cache_write_input_tokens", "reasoning_output_tokens",
))
_TERMINAL_TYPES: Final = frozenset(("turn.completed", "turn.failed", "turn.cancelled", "error"))
_PRIVATE_TMP_PREFIX: Final = "ffs-codex-"
_PRIVATE_TMP_SCHEMA: Final = "ffs.codex-private-tmpdir/v1"


class CodexHostRefused(ValueError):
    """The requested runtime, command material, or telemetry is not usable."""


class TelemetryRefused(CodexHostRefused):
    """The CLI stream cannot safely settle an invocation."""


@dataclass(frozen=True)
class CodexTelemetry:
    """The bounded terminal usage record retained from one Codex JSONL stream."""

    sha256: str
    byte_length: int
    thread_id: str
    token_usage: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CodexInvocationReceipt:
    """Immutable description of one attempt and whether it can be accounted for."""

    binary: tuple[tuple[str, str], ...]
    version: str
    argv: tuple[str, ...]
    cwd: str
    model: str
    effort: str
    runtime: QualifiedCodexRuntime
    config_sha256: str
    exit_code: int | None
    stream: CodexTelemetry | None
    attempt: int
    status: str
    uncertainty: str | None = None
    runtime_sha256: str = ""
    environment_sha256: str = ""


@dataclass(frozen=True)
class CodexLaunchMaterial:
    """The sole process material a Supervisor may use for one Codex attempt.

    ``temporary_dir`` is part of the closed environment and must be released
    with ``release_launch_material`` after the Supervisor closes the child.
    ``temporary_record`` names its durable orphan record, when one was written.
    """

    binary: tuple[tuple[str, str], ...]
    version: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: str
    model: str
    effort: str
    runtime: QualifiedCodexRuntime
    config_sha256: str
    attempt: int
    temporary_dir: str
    temporary_device: int
    temporary_inode: int
    auth_path: str
    auth_sha256: str
    auth_device: int
    auth_inode: int
    runtime_sha256: str = ""
    temporary_record: str = ""

    def execution_environment(self) -> dict[str, str]:
        """Return a fresh mapping so no caller can mutate this material."""
        return dict(self.environment)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TelemetryRefused("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _bounded_nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_INTEGER:
        raise TelemetryRefused(f"INVALID_{label}")
    return value


def parse_codex_telemetry(stream: bytes) -> CodexTelemetry:
    """Parse a complete Codex JSONL response with no best-effort recovery."""
    if not isinstance(stream, bytes) or len(stream) > _MAX_TELEMETRY_BYTES:
        raise TelemetryRefused("TELEMETRY_TOO_LARGE")
    try:
        text = stream.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TelemetryRefused("TELEMETRY_NOT_UTF8") from error
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, RecursionError, TelemetryRefused) as error:
            raise TelemetryRefused("TELEMETRY_NOT_JSONL") from error
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            raise TelemetryRefused("TELEMETRY_RECORD_INVALID")
        records.append(record)
    if not records:
        raise TelemetryRefused("TELEMETRY_MISSING")
    thread_records = [record for record in records if record["type"] == "thread.started"]
    terminals = [record for record in records if record["type"] in _TERMINAL_TYPES]
    if len(thread_records) != 1 or len(terminals) != 1 or terminals[0]["type"] != "turn.completed":
        raise TelemetryRefused("TELEMETRY_TERMINAL_AMBIGUOUS")
    if any(record["type"].startswith("turn.") and record["type"] not in {"turn.started", "turn.completed"}
           for record in records):
        raise TelemetryRefused("TELEMETRY_TERMINAL_AMBIGUOUS")
    thread_id = thread_records[0].get("thread_id")
    if not isinstance(thread_id, str) or not thread_id or "\0" in thread_id or len(thread_id) > 256:
        raise TelemetryRefused("THREAD_ID_INVALID")
    usage = terminals[0].get("usage")
    if not isinstance(usage, dict) or set(usage) != _TOKEN_FIELDS:
        raise TelemetryRefused("TOKEN_FIELDS_INVALID")
    values = tuple(sorted((field, _bounded_nonnegative(usage[field], "TOKEN_USAGE")) for field in _TOKEN_FIELDS))
    return CodexTelemetry(hashlib.sha256(stream).hexdigest(), len(stream), thread_id, values)


def _mapping(pairs: tuple[tuple[str, object], ...], label: str) -> dict[str, object]:
    try:
        result = dict(pairs)
    except (TypeError, ValueError) as error:
        raise CodexHostRefused(f"QUALIFIED_{label}_INVALID") from error
    if len(result) != len(pairs):
        raise CodexHostRefused(f"QUALIFIED_{label}_INVALID")
    return result


def _runtime_sha256(runtime: QualifiedCodexRuntime) -> str:
    """Pin the serializable qualification snapshot retained by a receipt."""
    try:
        raw = json.dumps(runtime.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CodexHostRefused("QUALIFIED_RUNTIME_INVALID") from error
    return hashlib.sha256(raw).hexdigest()


def _private_directory(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as error:
        raise CodexHostRefused(f"{label}_UNAVAILABLE") from error
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
        raise CodexHostRefused(f"{label}_UNSAFE")
    if info.st_mode & 0o077:
        raise CodexHostRefused(f"{label}_UNSAFE")
    return path.resolve()


def _regular_digest(path: Path, label: str) -> str:
    try:
        info = path.lstat()
        content = path.read_bytes()
    except OSError as error:
        raise CodexHostRefused(f"{label}_UNAVAILABLE") from error
    if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid():
        raise CodexHostRefused(f"{label}_UNSAFE")
    return hashlib.sha256(content).hexdigest()


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _ancestry(path: Path) -> set[tuple[int, int]]:
    """The (st_dev, st_ino) of ``path`` and of every existing ancestor."""
    return {identity for identity in map(_identity, (path, *path.parents)) if identity is not None}


def _overlaps(first: Path, second: Path) -> bool:
    """Either directory contains the other, lexically or by filesystem identity.

    A case-insensitive volume or a firmlink gives one directory several
    spellings; only (st_dev, st_ino) proves two of them are the same place.
    """
    first, second = first.resolve(), second.resolve()
    if first.is_relative_to(second) or second.is_relative_to(first):
        return True
    first_id, second_id = _identity(first), _identity(second)
    return ((second_id is not None and second_id in _ancestry(first))
            or (first_id is not None and first_id in _ancestry(second)))


def _discard(temporary: Path, record: Path | None) -> None:
    """Remove a private TMPDIR; drop its record only once the directory is gone."""
    shutil.rmtree(temporary, ignore_errors=True)
    if record is not None and not os.path.lexists(temporary):
        record.unlink(missing_ok=True)


def reap_orphan_private_tmpdirs(records: Path,
                                launch_dead: Callable[[str, str, tuple[int, int]], bool]) -> tuple[str, ...]:
    """Remove recorded private TMPDIRs whose owner and launch are provably dead.

    A supervisor killed mid-run leaves its launches' directories behind.  Only
    a directory this module recorded is a candidate, and only when the
    recording supervisor probes DEAD, ``launch_dead(activity_id, path,
    (st_dev, st_ino))`` proves durable state bound exactly that directory to a
    launch that is dead, and the path still names that identity.  A record is
    only a claim: unrecorded, unbound or replaced directories are never touched.
    Returns the reaped paths.
    """
    try:
        if records.is_symlink() or not records.is_dir():
            return ()
        entries = sorted(records.glob(_PRIVATE_TMP_PREFIX + "*.json"))[:4096]
    except OSError:
        return ()
    root, reaped = codex_private_tmp_root(), []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            record = json.loads(entry.read_text(encoding="utf-8"))
            path, identity = Path(record["path"]), (record["device"], record["inode"])
            if (record["schema"] != _PRIVATE_TMP_SCHEMA or path.parent != root
                    or entry.name != path.name + ".json" or not path.name.startswith(_PRIVATE_TMP_PREFIX)
                    or any(type(value) is not int for value in identity)
                    or probe_identity(ProcessIdentity(**record["owner"])) != DEAD
                    or not launch_dead(record["activity_id"], str(path), identity)):
                continue
        except Exception:
            # An unreadable, foreign or undecidable record is never proof.
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            entry.unlink(missing_ok=True)
            continue
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and (info.st_dev, info.st_ino) == identity:
            _discard(path, entry)
            if not os.path.lexists(path):
                reaped.append(str(path))
        else:
            # The recorded directory is gone; whatever holds its name now is not ours.
            entry.unlink(missing_ok=True)
    return tuple(reaped)


def verify_artifact_review_session(runtime: Path, *, thread_id: str, workspace: Path,
                                   model: str, effort: str) -> dict[str, str]:
    """Bind an artifact review to actual native model/turn and zero tool calls.

    This verifies retained evidence; it does not replace pre-launch tool
    restrictions, sandbox qualification, or the supervisor's launch authority.
    """
    if not isinstance(thread_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id) is None:
        raise CodexHostRefused("ARTIFACT_REVIEW_MODEL_UNPROVEN")
    runtime = _private_directory(Path(runtime), "RUNTIME")
    sessions = runtime / "sessions"
    try:
        if sessions.is_symlink() or not sessions.is_dir():
            raise ValueError
        candidates = []
        for count, path in enumerate(sessions.rglob("*")):
            if count >= 4096 or len(path.relative_to(sessions).parts) > 16 or path.is_symlink():
                raise ValueError
            if path.name.startswith("rollout-") and path.name.endswith(thread_id + ".jsonl"):
                candidates.append(path)
        if len(candidates) != 1:
            raise ValueError
        captured = _native_jsonl(candidates[0], allow_readable=True)
        if captured is None:
            raise ValueError
        records, session_hash = captured
        metadata = [entry.get("payload") for entry in records if entry.get("type") == "session_meta"]
        contexts = [entry.get("payload") for entry in records if entry.get("type") == "turn_context"]
        cwd = str(Path(workspace).resolve(strict=True))
        if (len(metadata) != 1 or not isinstance(metadata[0], dict)
                or metadata[0].get("id") != thread_id or metadata[0].get("cwd") != cwd
                or len(contexts) != 1 or not isinstance(contexts[0], dict)):
            raise ValueError
        context = contexts[0]
        if (context.get("cwd") != cwd or context.get("model") != model or context.get("effort") != effort
                or not isinstance(context.get("turn_id"), str) or not context["turn_id"]):
            raise ValueError
        for entry in records:
            if entry.get("type") == "response_item":
                payload = entry.get("payload")
                if not isinstance(payload, dict) or payload.get("type") not in {"message", "reasoning"}:
                    raise CodexHostRefused("ARTIFACT_REVIEW_TOOL_USE")
        return {"session_sha256": session_hash, "thread_id": thread_id,
                "turn_id": context["turn_id"], "effective_model": model, "effective_effort": effort}
    except CodexHostRefused:
        raise
    except (OSError, ValueError) as error:
        raise CodexHostRefused("ARTIFACT_REVIEW_MODEL_UNPROVEN") from error


class CodexHostAdapter:
    """Run one prompt through the immutable, qualified Codex configuration."""

    def __init__(self, runtime: QualifiedCodexRuntime, binary: str | Path, version: str,
                 gsd_environment: object = None, *, state_root: str | Path | None = None,
                 tmp_records: str | Path | None = None, activity_id: str = "") -> None:
        """``state_root`` is kept disjoint from each launch's private TMPDIR;
        ``tmp_records`` (with its ``activity_id``) receives the durable orphan record."""
        if not isinstance(runtime, QualifiedCodexRuntime):
            raise CodexHostRefused("RUNTIME_UNQUALIFIED")
        if not isinstance(version, str) or not version or "\0" in version or len(version) > 128:
            raise CodexHostRefused("VERSION_INVALID")
        if tmp_records is not None and (not isinstance(activity_id, str) or not activity_id):
            raise CodexHostRefused("PRIVATE_TMPDIR_RECORD_INVALID")
        self.runtime = runtime
        self.binary = Path(binary)
        self.version = version
        self.state_root = None if state_root is None else Path(state_root)
        self.tmp_records = None if tmp_records is None else Path(tmp_records)
        self.activity_id = activity_id
        if gsd_environment is not None:
            try:
                self.gsd_environment = validate_gsd_supervisor_environment(gsd_environment)
            except CapabilityError as error:
                raise CodexHostRefused("GSD_ENVIRONMENT_INVALID") from error
        else:
            self.gsd_environment = None

    def _gsd_environment(self, value: object = None) -> GsdSupervisorEnvironment | None:
        candidate = self.gsd_environment if value is None else value
        if candidate is None:
            try:
                return gsd_supervisor_environment_from_process()
            except CapabilityError as error:
                raise CodexHostRefused("GSD_ENVIRONMENT_INVALID") from error
        try:
            return validate_gsd_supervisor_environment(candidate)
        except CapabilityError as error:
            raise CodexHostRefused("GSD_ENVIRONMENT_INVALID") from error

    def _material(self) -> tuple[Path, Path, str, str, str, tuple[tuple[str, str], ...]]:
        runtime = _mapping(self.runtime.runtime, "RUNTIME")
        execution = _mapping(self.runtime.execution, "EXECUTION")
        workspace = _mapping(self.runtime.workspace, "WORKSPACE")
        binary = _mapping(self.runtime.binary, "BINARY")
        home_value, cwd_value = runtime.get("path"), workspace.get("path")
        if not isinstance(home_value, str) or not isinstance(cwd_value, str):
            raise CodexHostRefused("QUALIFIED_PATH_INVALID")
        home, cwd = _private_directory(Path(home_value), "RUNTIME_HOME"), Path(cwd_value).resolve()
        if cwd.is_symlink() or not cwd.is_dir():
            raise CodexHostRefused("WORKSPACE_UNSAFE")
        if (workspace.get("device") != cwd.stat().st_dev
                or workspace.get("inode") != cwd.stat().st_ino):
            raise CodexHostRefused("WORKSPACE_DRIFT")
        config_sha256 = runtime.get("config_sha256")
        if not isinstance(config_sha256, str) or _regular_digest(home / "config.toml", "RUNTIME_CONFIG") != config_sha256:
            raise CodexHostRefused("RUNTIME_CONFIG_DRIFT")
        model, effort, sandbox = execution.get("model"), execution.get("effort"), execution.get("sandbox")
        if (not isinstance(model, str) or not isinstance(effort, str)
                or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", model) is None
                or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", effort) is None
                or sandbox not in {"read-only", "workspace-write", "danger-full-access"}):
            raise CodexHostRefused("QUALIFIED_EXECUTION_INVALID")
        if execution.get("network_enabled") is not False or execution.get("roots") != [str(cwd)]:
            raise CodexHostRefused("QUALIFIED_WORKSPACE_INVALID")
        if execution.get("disabled_features") != list(DISABLED_NATIVE_FEATURES):
            raise CodexHostRefused("QUALIFIED_NATIVE_FEATURES_INVALID")
        try:
            observed_binary = _binary_chain(self.binary)
        except (OSError, ValueError) as error:
            raise CodexHostRefused("BINARY_UNSAFE") from error
        if observed_binary != binary:
            raise CodexHostRefused("BINARY_DRIFT")
        return home, cwd, model, effort, sandbox, tuple(sorted(observed_binary.items()))

    def _private_tmpdir(self, home: Path, cwd: Path) -> tuple[Path, Path | None]:
        """Create and record this launch's 0700 TMPDIR outside every protected root."""
        try:
            primary = _canonical_git_worktree(cwd)
        except CapabilityError as error:
            raise CodexHostRefused("PRIVATE_TMPDIR_UNSAFE") from error
        protected = [cwd, home, Path.home(), *(() if primary is None else (primary, primary / ".git")),
                     *(() if self.state_root is None else (self.state_root,))]
        root = codex_private_tmp_root()
        try:
            temporary = Path(tempfile.mkdtemp(prefix=_PRIVATE_TMP_PREFIX, dir=root))
        except OSError as error:
            raise CodexHostRefused("PRIVATE_TMPDIR_UNAVAILABLE") from error
        record = None
        try:
            info = temporary.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700 or temporary.parent != root
                    or any(_overlaps(temporary, item) for item in protected)):
                raise CodexHostRefused("PRIVATE_TMPDIR_UNSAFE")
            if self.tmp_records is not None:
                record = self._record_private_tmpdir(temporary, info)
        except BaseException:
            _discard(temporary, record)
            raise
        return temporary, record

    def _record_private_tmpdir(self, temporary: Path, info: os.stat_result) -> Path:
        """Durably name the directory, its identity and its owner before any launch.

        Only a record this call created is ever unlinked on failure; a name
        that already exists is refused and left exactly as found.
        """
        record = self.tmp_records / (temporary.name + ".json")
        value = {"schema": _PRIVATE_TMP_SCHEMA, "path": str(temporary), "device": info.st_dev,
                 "inode": info.st_ino, "activity_id": self.activity_id}
        try:
            value["owner"] = asdict(ProcessIdentity.current())
            record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _private_directory(record.parent, "PRIVATE_TMPDIR_RECORDS")
            descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise CodexHostRefused("PRIVATE_TMPDIR_RECORD_UNAVAILABLE") from error
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException as error:
            record.unlink(missing_ok=True)
            if isinstance(error, OSError):
                raise CodexHostRefused("PRIVATE_TMPDIR_RECORD_UNAVAILABLE") from error
            raise
        return record

    @staticmethod
    def _argv(binary: Path, model: str, effort: str, sandbox: str, cwd: Path, prompt: str,
              tmpdir: Path) -> tuple[str, ...]:
        disabled = (
            "multi_agent", "multi_agent_v2", "plugins", "remote_plugin",
            "recommended_plugins", "plugin_sharing", "apps",
        )
        disabled_argv = tuple(item for feature in disabled for item in ("--disable", feature))
        sandbox_argv = (
            "-c", "sandbox_workspace_write.network_access=false",
            "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        )
        return (
            str(binary.resolve()), "exec", "--json", "-c", f'model="{model}"', "-c",
            f'model_reasoning_effort="{effort}"', "--strict-config", "--ignore-user-config",
            "--ignore-rules", "--dangerously-bypass-hook-trust", "--sandbox", sandbox,
            # F43: the launch's private TMPDIR is its one extra writable root;
            # both exclusions stay on, so /tmp and $TMPDIR are never granted wholesale.
            *sandbox_argv, *disabled_argv, "--add-dir", str(tmpdir), "--cd", str(cwd),
            "--color", "never", prompt,
        )

    @staticmethod
    def _environment(home: Path, tmpdir: Path, binary: Path, chain: tuple[tuple[str, str], ...],
                     gsd_environment: GsdSupervisorEnvironment | None = None) -> dict[str, str]:
        try:
            return codex_closed_environment(home, tmpdir, binary, chain, gsd_environment)
        except CapabilityError as error:
            raise CodexHostRefused("BINARY_DRIFT") from error

    def build_launch_material(self, prompt: str, *, attempt: int,
                              gsd_environment: object = None) -> CodexLaunchMaterial:
        """Build immutable launch material; this method never starts a process."""
        if not isinstance(prompt, str) or not prompt or "\0" in prompt or len(prompt.encode("utf-8")) > 64 * 1024:
            raise CodexHostRefused("PROMPT_INVALID")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or not 0 <= attempt <= _MAX_INTEGER:
            raise CodexHostRefused("ATTEMPT_INVALID")
        home, cwd, model, effort, sandbox, binary = self._material()
        additions = self._gsd_environment(gsd_environment)
        temporary, record = self._private_tmpdir(home, cwd)
        try:
            environment = self._environment(home, temporary, self.binary, binary, additions)
            observation = _mapping(self.runtime.observation, "OBSERVATION")
            observed_policy = observation.get("environment_sha256")
            if (not isinstance(observed_policy, str)
                    or re.fullmatch(r"[0-9a-f]{64}", observed_policy) is None):
                raise CodexHostRefused("QUALIFIED_OBSERVATION_INVALID")
            try:
                expected_policy = codex_environment_policy_hash(environment)
            except CapabilityError as error:
                raise CodexHostRefused("ENVIRONMENT_POLICY_INVALID") from error
            if observed_policy != expected_policy:
                # Also a pre-F43 observation, whose policy TMPDIR was the runtime home.
                raise CodexHostRefused("ENVIRONMENT_POLICY_DRIFT")
            temporary_info = temporary.stat()
            auth = home / "auth.json"
            try:
                auth_info = auth.lstat()
                auth_sha256 = hashlib.sha256(auth.read_bytes()).hexdigest()
            except OSError as error:
                raise CodexHostRefused("RUNTIME_AUTH_UNAVAILABLE") from error
            if (auth.is_symlink() or not auth.is_file() or auth_info.st_uid != os.getuid()
                    or auth_info.st_nlink != 1 or stat.S_IMODE(auth_info.st_mode) != 0o600):
                raise CodexHostRefused("RUNTIME_AUTH_UNSAFE")
        except BaseException:
            _discard(temporary, record)
            raise
        return CodexLaunchMaterial(binary, self.version,
                                   self._argv(self.binary, model, effort, sandbox, cwd, prompt, temporary),
                                   tuple(sorted(environment.items())), str(cwd), model, effort, self.runtime,
                                   _mapping(self.runtime.runtime, "RUNTIME")["config_sha256"], attempt, str(temporary),
                                   temporary_info.st_dev, temporary_info.st_ino, str(auth), auth_sha256,
                                   auth_info.st_dev, auth_info.st_ino, _runtime_sha256(self.runtime),
                                   "" if record is None else str(record))

    @staticmethod
    def release_launch_material(material: CodexLaunchMaterial) -> None:
        """Remove only the private temporary directory allocated by this adapter.

        Material whose directory sits anywhere but the private root (a pre-F43
        runtime-home leaf included) is refused and left in place.
        """
        if not isinstance(material, CodexLaunchMaterial):
            raise CodexHostRefused("LAUNCH_MATERIAL_INVALID")
        temporary = Path(material.temporary_dir)
        record = Path(material.temporary_record) if material.temporary_record else None
        if (not temporary.is_absolute() or temporary.parent != codex_private_tmp_root()
                or not temporary.name.startswith(_PRIVATE_TMP_PREFIX)
                or (record is not None and record.name != temporary.name + ".json")):
            raise CodexHostRefused("LAUNCH_MATERIAL_INVALID")
        try:
            info = temporary.lstat()
        except OSError as error:
            raise CodexHostRefused("LAUNCH_MATERIAL_INVALID") from error
        if (temporary.is_symlink() or not temporary.is_dir() or info.st_uid != os.getuid()
                or (info.st_dev, info.st_ino) != (material.temporary_device, material.temporary_inode)):
            raise CodexHostRefused("LAUNCH_MATERIAL_INVALID")
        _discard(temporary, record)

    def invoke(self, prompt: str, *, attempt: int, timeout_seconds: int = 600,
               gsd_environment: object = None) -> CodexInvocationReceipt:
        """Test helper that executes built material; production dispatch uses the Supervisor."""
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 0 < timeout_seconds <= 3600:
            raise CodexHostRefused("TIMEOUT_INVALID")
        material = self.build_launch_material(prompt, attempt=attempt, gsd_environment=gsd_environment)
        environment_sha256 = hashlib.sha256(
            json.dumps(dict(material.environment), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        try:
            try:
                completed = subprocess.run(material.argv, cwd=material.cwd, env=material.execution_environment(), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           timeout=timeout_seconds, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                return CodexInvocationReceipt(material.binary, material.version, material.argv, material.cwd,
                                               material.model, material.effort, material.runtime, material.config_sha256,
                                               None, None, material.attempt, "uncertain", type(error).__name__,
                                               material.runtime_sha256, environment_sha256)
            try:
                telemetry = parse_codex_telemetry(completed.stdout)
            except TelemetryRefused as error:
                return CodexInvocationReceipt(material.binary, material.version, material.argv, material.cwd,
                                               material.model, material.effort, material.runtime, material.config_sha256,
                                               completed.returncode, None, material.attempt, "uncertain", str(error),
                                               material.runtime_sha256, environment_sha256)
            status = "completed" if completed.returncode == 0 else "failed"
            return CodexInvocationReceipt(material.binary, material.version, material.argv, material.cwd,
                                           material.model, material.effort, material.runtime, material.config_sha256,
                                           completed.returncode, telemetry, material.attempt, status, None,
                                           material.runtime_sha256, environment_sha256)
        finally:
            self.release_launch_material(material)
