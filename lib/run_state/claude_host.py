"""Closed Claude Code subscription transport for supervisor-owned launches."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Final
import uuid

from host_capabilities import (
    CapabilityError, GsdSupervisorEnvironment, _binary_chain,
    gsd_supervisor_environment_from_process, validate_gsd_supervisor_environment,
)
from model_requests import ModelRequestError, resolve_request


CLAUDE_TELEMETRY_SCHEMA: Final = "ffs.claude-runtime-telemetry/v1"
QUALIFIED_CLAUDE_RUNTIME_SCHEMA: Final = "ffs.qualified-claude-runtime/v1"
SUPPORTED_CLAUDE_VERSION: Final = "2.1.274"
_MAX_STREAM: Final = 4 * 1024 * 1024
_TOKEN_FIELDS: Final = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
_BASE_ENVIRONMENT: Final = frozenset({
    "HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "PATH", "LANG", "LC_ALL", "NO_COLOR", "CI",
    "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
})
_GSD_ENVIRONMENT: Final = frozenset({
    "GSD_DISPATCH_MODE", "FFS_SUPERVISED_COMMIT_MODE", "FFS_SUPERVISED_ADMISSION_FILE",
    "FFS_SUPERVISED_DISPATCH_COMMAND_JSON",
})
_TOOLS: Final = "Bash,Edit,Glob,Grep,Read,Skill,Write"


class ClaudeHostRefused(ValueError):
    pass


class ClaudeTelemetryRefused(ClaudeHostRefused):
    pass


@dataclass(frozen=True)
class ClaudeHostRequest:
    runtime_home: str
    credential_source: str
    binary: str
    model: str
    effort: str | None
    sandbox: str
    network_enabled: bool
    token_reservation: int
    timeout_seconds: int

    def material(self) -> dict[str, object]:
        return {"host": "claude", **asdict(self)}


@dataclass(frozen=True)
class QualifiedClaudeRuntime:
    binary: tuple[tuple[str, str], ...]
    runtime: tuple[tuple[str, object], ...]
    workspace: tuple[tuple[str, object], ...]
    supervisor: tuple[tuple[str, object], ...]
    execution: tuple[tuple[str, object], ...]
    observation: tuple[tuple[str, object], ...]

    @property
    def status(self) -> str:
        return "admitted"

    def to_dict(self) -> dict[str, object]:
        return json.loads(json.dumps({
            "schema": QUALIFIED_CLAUDE_RUNTIME_SCHEMA, "status": self.status,
            "binary": dict(self.binary), "runtime": dict(self.runtime),
            "workspace": dict(self.workspace), "supervisor": dict(self.supervisor),
            "execution": dict(self.execution), "observation": dict(self.observation),
        }, sort_keys=True))


@dataclass(frozen=True)
class ClaudeTelemetry:
    schema: str
    sha256: str
    byte_length: int
    session_id: str
    effective_model: str
    cli_version: str
    token_usage: tuple[tuple[str, int], ...]
    hook_events: tuple[str, ...]


@dataclass(frozen=True)
class ClaudeLaunchMaterial:
    binary: tuple[tuple[str, str], ...]
    version: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: str
    model: str
    effort: str | None
    session_id: str
    runtime: QualifiedClaudeRuntime
    attempt: int
    temporary_dir: str
    temporary_device: int
    temporary_inode: int
    credential_path: str
    credential_sha256: str
    credential_device: int
    credential_inode: int
    environment_sha256: str
    runtime_sha256: str

    def execution_environment(self) -> dict[str, str]:
        return dict(self.environment)


def parse_claude_host_request(*, runtime_home: str, credential_source: str, binary: str,
                              model_request_json: str, sandbox: str, network_enabled: bool,
                              token_reservation: int, timeout_seconds: int) -> ClaudeHostRequest:
    try:
        resolved = resolve_request(json.loads(model_request_json), host="claude")
        home, credential, executable = Path(runtime_home), Path(credential_source), Path(binary)
    except (json.JSONDecodeError, ModelRequestError, TypeError, ValueError) as error:
        raise ClaudeHostRefused("HOST_MODEL_REQUEST_INVALID") from error
    if (not all(path.is_absolute() and path.resolve() == path for path in (home, credential, executable))
            or sandbox != "workspace-write" or type(network_enabled) is not bool
            or network_enabled is not False or type(token_reservation) is not int
            or not 0 <= token_reservation <= 2**63 - 1 or type(timeout_seconds) is not int
            or not 0 < timeout_seconds <= 3600 or not isinstance(resolved.get("model"), str)
            or not resolved["model"] or resolved.get("effort") not in {None, "low", "medium", "high", "xhigh", "max"}):
        raise ClaudeHostRefused("HOST_REQUEST_INVALID")
    return ClaudeHostRequest(str(home), str(credential), str(executable), resolved["model"],
                             resolved.get("effort"), sandbox, False, token_reservation, timeout_seconds)


def _pairs(value: tuple[tuple[str, object], ...], label: str) -> dict[str, object]:
    try:
        result = dict(value)
    except (TypeError, ValueError) as error:
        raise ClaudeHostRefused(f"QUALIFIED_{label}_INVALID") from error
    if len(result) != len(value):
        raise ClaudeHostRefused(f"QUALIFIED_{label}_INVALID")
    return result


def _digest(path: Path, label: str) -> str:
    try:
        info, data = path.lstat(), path.read_bytes()
    except OSError as error:
        raise ClaudeHostRefused(f"{label}_UNAVAILABLE") from error
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ClaudeHostRefused(f"{label}_UNSAFE")
    return hashlib.sha256(data).hexdigest()


def _runtime_sha256(runtime: QualifiedClaudeRuntime) -> str:
    return hashlib.sha256(json.dumps(runtime.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_stage_closure(home: Path, expected_sha256: object) -> None:
    manifest_path = home / "runtime-stage-manifest.json"
    if not isinstance(expected_sha256, str) or _digest(manifest_path, "RUNTIME_STAGE") != expected_sha256:
        raise ClaudeHostRefused("RUNTIME_STAGE_DRIFT")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_duplicate_guard)
        target = manifest["target"]
        identity, files = target["home"], target["files"]
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, KeyError, TypeError,
            ClaudeTelemetryRefused) as error:
        raise ClaudeHostRefused("RUNTIME_STAGE_INVALID") from error
    info = home.stat()
    if (manifest.get("schema") != "ffs.private-claude-runtime-stage/v1" or not isinstance(identity, dict)
            or identity.get("path") != str(home.resolve()) or identity.get("device") != info.st_dev
            or identity.get("inode") != info.st_ino or not isinstance(files, dict) or not files):
        raise ClaudeHostRefused("RUNTIME_STAGE_INVALID")
    for relative, expected in files.items():
        pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath("..")
        if (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None
                or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts)
                or str(pure) != relative):
            raise ClaudeHostRefused("RUNTIME_STAGE_INVALID")
        if _digest(home.joinpath(*pure.parts), "RUNTIME_STAGE_FILE") != expected:
            raise ClaudeHostRefused("RUNTIME_STAGE_DRIFT")


def claude_closed_environment(home: Path, tmpdir: Path, binary: Path,
                              gsd_environment: object = None) -> dict[str, str]:
    paths = [str(binary.resolve().parent), "/usr/local/bin", "/usr/bin", "/bin"]
    environment = {
        "HOME": str(home.resolve().parent), "CLAUDE_CONFIG_DIR": str(home.resolve()),
        "TMPDIR": str(tmpdir.resolve()), "PATH": os.pathsep.join(dict.fromkeys(paths)),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1", "CI": "1",
        "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    }
    if gsd_environment is not None:
        environment.update(validate_gsd_supervisor_environment(gsd_environment).as_dict())
    return environment


def claude_environment_policy(environment: dict[str, str], *, preview: bool = False) -> dict[str, str]:
    allowed = _BASE_ENVIRONMENT | (_GSD_ENVIRONMENT if _GSD_ENVIRONMENT.issubset(environment) else frozenset())
    if set(environment) != allowed or any(not isinstance(value, str) for value in environment.values()):
        raise ClaudeHostRefused("CLAUDE_ENVIRONMENT_INVALID")
    for key in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR"):
        if not Path(environment[key]).is_absolute():
            raise ClaudeHostRefused("CLAUDE_ENVIRONMENT_INVALID")
    policy = dict(environment)
    policy["TMPDIR"] = str(Path(environment["TMPDIR"]).resolve().parent / "<invocation>")
    if _GSD_ENVIRONMENT.issubset(policy):
        additions = {key: policy[key] for key in _GSD_ENVIRONMENT}
        if preview:
            admission = Path(additions["FFS_SUPERVISED_ADMISSION_FILE"])
            if not admission.is_absolute():
                raise ClaudeHostRefused("CLAUDE_ENVIRONMENT_INVALID")
            policy["FFS_SUPERVISED_ADMISSION_FILE"] = str(admission.resolve().parent / "<admission>")
            command = additions["FFS_SUPERVISED_DISPATCH_COMMAND_JSON"]
            try:
                if json.dumps(json.loads(command), separators=(",", ":")) != command:
                    raise ValueError
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                raise ClaudeHostRefused("CLAUDE_ENVIRONMENT_INVALID") from error
        else:
            try:
                valid = validate_gsd_supervisor_environment(additions)
            except CapabilityError as error:
                raise ClaudeHostRefused("CLAUDE_ENVIRONMENT_INVALID") from error
            policy["FFS_SUPERVISED_ADMISSION_FILE"] = str(Path(valid.admission_file).resolve().parent / "<admission>")
    return policy


def claude_environment_policy_hash(environment: dict[str, str], *, preview: bool = False) -> str:
    return hashlib.sha256(json.dumps(claude_environment_policy(environment, preview=preview),
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _duplicate_guard(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ClaudeTelemetryRefused("DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def parse_claude_telemetry(stream: bytes, *, requested_model: str,
                           expected_session_id: str,
                           expected_version: str | None = None,
                           artifact_review_workspace: str | None = None) -> ClaudeTelemetry:
    if not isinstance(stream, bytes) or len(stream) > _MAX_STREAM:
        raise ClaudeTelemetryRefused("TELEMETRY_TOO_LARGE")
    try:
        text = stream.decode("utf-8")
    except UnicodeError as error:
        raise ClaudeTelemetryRefused("TELEMETRY_NOT_UTF8") from error
    records: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line, object_pairs_hook=_duplicate_guard)
        except (json.JSONDecodeError, RecursionError, ClaudeTelemetryRefused) as error:
            raise ClaudeTelemetryRefused("TELEMETRY_NOT_JSONL") from error
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ClaudeTelemetryRefused("TELEMETRY_RECORD_INVALID")
        records.append(value)
    initial = [item for item in records if item.get("type") == "system" and item.get("subtype") == "init"]
    terminal = [item for item in records if item.get("type") == "result"]
    if len(initial) != 1 or len(terminal) != 1 or records[-1] is not terminal[0]:
        raise ClaudeTelemetryRefused("TELEMETRY_TERMINAL_AMBIGUOUS")
    init, result = initial[0], terminal[0]
    session_id, effective = init.get("session_id"), init.get("model")
    if session_id != expected_session_id or result.get("session_id") != expected_session_id:
        raise ClaudeTelemetryRefused("SESSION_ID_MISMATCH")
    if effective != requested_model:
        raise ClaudeTelemetryRefused("EFFECTIVE_MODEL_MISMATCH")
    cli_version = init.get("claude_code_version")
    if not isinstance(cli_version, str) or not cli_version:
        raise ClaudeTelemetryRefused("CLI_VERSION_MISSING")
    if expected_version is not None and cli_version != expected_version:
        raise ClaudeTelemetryRefused("CLI_VERSION_MISMATCH")
    if result.get("is_error") is not False or result.get("subtype") not in {"success", "success_max_turns"}:
        raise ClaudeTelemetryRefused("TELEMETRY_RESULT_FAILED")
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise ClaudeTelemetryRefused("TOKEN_USAGE_INVALID")
    tokens: list[tuple[str, int]] = []
    for field in _TOKEN_FIELDS:
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
            raise ClaudeTelemetryRefused("TOKEN_USAGE_INVALID")
        tokens.append((field, value))
    hooks = sorted({str(item.get("hook_name") or item.get("hook_event")) for item in records
                    if item.get("type") == "system" and str(item.get("subtype", "")).startswith("hook_")
                    and (item.get("hook_name") or item.get("hook_event"))})
    if artifact_review_workspace is not None:
        _validate_artifact_review_records(records, workspace=artifact_review_workspace,
                                          model=requested_model, session_id=expected_session_id)
    return ClaudeTelemetry(CLAUDE_TELEMETRY_SCHEMA, hashlib.sha256(stream).hexdigest(), len(stream),
                           expected_session_id, requested_model, cli_version, tuple(tokens), tuple(hooks))


def _validate_artifact_review_records(records, *, workspace, model, session_id):
    """Additional pinned review restrictions; ordinary worker parsing is unchanged.

    An empty observed registry and no tool records complement the pre-launch
    restrictions; neither alone is a restricted-profile qualification proof.
    Unknown record/content forms fail closed in this opt-in review mode.
    """
    init, result = records[0], records[-1]
    if (init.get("type") != "system" or init.get("subtype") != "init"
            or init.get("cwd") != workspace
            or any(init.get(key) != [] for key in ("tools", "mcp_servers", "slash_commands", "skills", "plugins"))):
        raise ClaudeTelemetryRefused("ARTIFACT_REVIEW_REGISTRY_UNPROVEN")
    if (result.get("subtype") != "success" or result.get("stop_reason") != "end_turn"
            or type(result.get("num_turns")) is not int or result["num_turns"] != 1
            or result.get("permission_denials") != []):
        raise ClaudeTelemetryRefused("ARTIFACT_REVIEW_RESULT_UNPROVEN")
    assistants = records[1:-1]
    if not assistants:
        raise ClaudeTelemetryRefused("ARTIFACT_REVIEW_OUTPUT_UNPROVEN")
    for record in assistants:
        message = record.get("message")
        if (record.get("type") != "assistant" or record.get("session_id") != session_id
                or record.get("parent_tool_use_id") is not None or not isinstance(message, dict)
                or message.get("role") != "assistant" or message.get("model") != model):
            raise ClaudeTelemetryRefused("ARTIFACT_REVIEW_OUTPUT_UNPROVEN")
        content = message.get("content")
        if (not isinstance(content, list) or not content
                or any(not isinstance(block, dict) or block.get("type") not in
                       {"text", "thinking", "redacted_thinking"} for block in content)):
            raise ClaudeTelemetryRefused("ARTIFACT_REVIEW_TOOL_USE")
    model_usage = result.get("modelUsage")
    if not isinstance(model_usage, dict) or set(model_usage) != {model}:
        raise ClaudeTelemetryRefused("ARTIFACT_REVIEW_MODEL_UNPROVEN")
    effective = model_usage[model]
    if (not isinstance(effective, dict) or effective.get("canonicalModel") != model
            or type(effective.get("webSearchRequests")) is not int or effective["webSearchRequests"] != 0):
        raise ClaudeTelemetryRefused("ARTIFACT_REVIEW_MODEL_UNPROVEN")
    server_tools = result["usage"].get("server_tool_use")
    if (not isinstance(server_tools, dict)
            or set(server_tools) != {"web_search_requests", "web_fetch_requests"}
            or any(type(count) is not int or count != 0 for count in server_tools.values())):
        raise ClaudeTelemetryRefused("ARTIFACT_REVIEW_TOOL_USE")


class ClaudeHostAdapter:
    def __init__(self, runtime: QualifiedClaudeRuntime, binary: str | Path, version: str,
                 gsd_environment: object = None) -> None:
        if not isinstance(runtime, QualifiedClaudeRuntime):
            raise ClaudeHostRefused("RUNTIME_UNQUALIFIED")
        if not isinstance(version, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version) is None:
            raise ClaudeHostRefused("VERSION_INVALID")
        self.runtime, self.binary, self.version = runtime, Path(binary), version
        self.gsd_environment = gsd_environment

    def _gsd(self, supplied: object = None) -> GsdSupervisorEnvironment | None:
        value = self.gsd_environment if supplied is None else supplied
        try:
            return gsd_supervisor_environment_from_process() if value is None else validate_gsd_supervisor_environment(value)
        except CapabilityError as error:
            raise ClaudeHostRefused("GSD_ENVIRONMENT_INVALID") from error

    def _material(self) -> tuple[Path, Path, str, str | None, tuple[tuple[str, str], ...]]:
        runtime, workspace = _pairs(self.runtime.runtime, "RUNTIME"), _pairs(self.runtime.workspace, "WORKSPACE")
        execution, binary = _pairs(self.runtime.execution, "EXECUTION"), _pairs(self.runtime.binary, "BINARY")
        home, cwd = Path(str(runtime.get("path", ""))), Path(str(workspace.get("path", "")))
        if not home.is_absolute() or not cwd.is_absolute() or home.is_symlink() or cwd.is_symlink():
            raise ClaudeHostRefused("QUALIFIED_PATH_INVALID")
        for path, label in ((home, "RUNTIME_HOME"), (cwd, "WORKSPACE")):
            try:
                info = path.stat()
            except OSError as error:
                raise ClaudeHostRefused(f"{label}_UNAVAILABLE") from error
            if not path.is_dir() or info.st_uid != os.getuid():
                raise ClaudeHostRefused(f"{label}_UNSAFE")
        info = cwd.stat()
        if workspace.get("device") != info.st_dev or workspace.get("inode") != info.st_ino:
            raise ClaudeHostRefused("WORKSPACE_DRIFT")
        settings_hash = runtime.get("settings_sha256")
        _validate_stage_closure(home, runtime.get("stage_sha256"))
        if settings_hash != _digest(home / "settings.json", "RUNTIME_SETTINGS"):
            raise ClaudeHostRefused("RUNTIME_SETTINGS_DRIFT")
        model, effort = execution.get("model"), execution.get("effort")
        if not isinstance(model, str) or not model or effort not in {None, "low", "medium", "high", "xhigh", "max"}:
            raise ClaudeHostRefused("QUALIFIED_EXECUTION_INVALID")
        if execution.get("sandbox") != "workspace-write" or execution.get("network_enabled") is not False:
            raise ClaudeHostRefused("QUALIFIED_EXECUTION_INVALID")
        try:
            observed = _binary_chain(self.binary)
        except (OSError, ValueError) as error:
            raise ClaudeHostRefused("BINARY_UNSAFE") from error
        if observed != binary:
            raise ClaudeHostRefused("BINARY_DRIFT")
        return home, cwd, model, effort, tuple(sorted(observed.items()))

    @staticmethod
    def _argv(binary: Path, cwd: Path, settings: Path, model: str, effort: str | None,
              session_id: str, prompt: str) -> tuple[str, ...]:
        result = [str(binary.resolve()), "-p", prompt, "--input-format", "text", "--output-format",
                  "stream-json", "--verbose", "--include-hook-events", "--model", model]
        if effort is not None:
            result.extend(("--effort", effort))
        result.extend(("--permission-mode", "acceptEdits", "--permission-prompts", "none",
                       "--no-session-persistence", "--no-chrome", "--strict-mcp-config",
                       "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "user", "--settings", str(settings),
                       "--session-id", session_id, "--add-dir", str(cwd), "--tools", _TOOLS))
        return tuple(result)

    def build_launch_material(self, prompt: str, *, attempt: int, session_id: str,
                              gsd_environment: object = None) -> ClaudeLaunchMaterial:
        if not isinstance(prompt, str) or not prompt or "\0" in prompt or len(prompt.encode()) > 64 * 1024:
            raise ClaudeHostRefused("PROMPT_INVALID")
        if type(attempt) is not int or not 0 <= attempt <= 2**63 - 1:
            raise ClaudeHostRefused("ATTEMPT_INVALID")
        try:
            if str(uuid.UUID(session_id)) != session_id:
                raise ValueError
        except (ValueError, AttributeError) as error:
            raise ClaudeHostRefused("SESSION_ID_INVALID") from error
        home, cwd, model, effort, binary = self._material()
        temporary = Path(tempfile.mkdtemp(prefix="ffs-claude-", dir=home))
        try:
            environment = claude_closed_environment(home, temporary, self.binary,
                                                    self._gsd(gsd_environment))
            expected = _pairs(self.runtime.observation, "OBSERVATION").get("environment_sha256")
            if expected != claude_environment_policy_hash(environment):
                raise ClaudeHostRefused("ENVIRONMENT_POLICY_DRIFT")
            credential = home / ".credentials.json"
            info = credential.lstat()
            digest = _digest(credential, "RUNTIME_CREDENTIAL")
            if (info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ClaudeHostRefused("RUNTIME_CREDENTIAL_UNSAFE")
            temp_info = temporary.stat()
            argv = self._argv(self.binary, cwd, home / "settings.json", model, effort, session_id, prompt)
            return ClaudeLaunchMaterial(binary, self.version, argv, tuple(sorted(environment.items())), str(cwd),
                                        model, effort, session_id, self.runtime, attempt, str(temporary),
                                        temp_info.st_dev, temp_info.st_ino, str(credential), digest,
                                        info.st_dev, info.st_ino, claude_environment_policy_hash(environment),
                                        _runtime_sha256(self.runtime))
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def release_launch_material(material: ClaudeLaunchMaterial) -> None:
        temporary = Path(material.temporary_dir)
        home = Path(str(_pairs(material.runtime.runtime, "RUNTIME").get("path", "")))
        try:
            info = temporary.lstat()
        except OSError as error:
            raise ClaudeHostRefused("LAUNCH_MATERIAL_INVALID") from error
        if (temporary.parent != home or not temporary.name.startswith("ffs-claude-") or temporary.is_symlink()
                or not temporary.is_dir() or (info.st_dev, info.st_ino) !=
                (material.temporary_device, material.temporary_inode)):
            raise ClaudeHostRefused("LAUNCH_MATERIAL_INVALID")
        shutil.rmtree(temporary)
