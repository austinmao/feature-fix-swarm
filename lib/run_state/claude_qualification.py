"""Fixed, supervisor-transportable qualification for the pinned Claude Code CLI."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
from typing import Final, NamedTuple
import uuid

from host_capabilities import _binary_chain, current_supervisor_identity
from .claude_host import (
    ClaudeHostAdapter, ClaudeHostRefused, QualifiedClaudeRuntime, SUPPORTED_CLAUDE_VERSION,
    claude_closed_environment, claude_environment_policy_hash, parse_claude_telemetry,
)
from .claude_runtime_staging import STAGE_MANIFEST_NAME, STAGE_SCHEMA


QUALIFICATION_PROBES: Final = ("auth-negative", "session-model", "sandbox-hooks", "nested-auth")
QUALIFICATION_SCHEMA: Final = "ffs.claude-runtime-qualification/v1"
_QUALIFIED_TOOLS: Final = "Bash,Edit,Glob,Grep,Read,Skill,Write"


class ClaudeQualificationRefused(ValueError):
    pass


class QualificationResult(NamedTuple):
    name: str
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class QualificationProbe:
    name: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: str
    session_id: str | None
    timeout_seconds: int
    contract_sha256: str
    credential_path: str | None
    credential_sha256: str | None
    credential_device: int | None
    credential_inode: int | None


@dataclass(frozen=True)
class QualificationPlan:
    runtime: Path
    binary: Path
    workspace: Path
    version: str
    model: str
    effort: str | None
    sandbox: str
    stage_sha256: str
    environment_sha256: str
    envelope_sha256: str
    output: Path
    outside_sentinel: Path
    inside_sentinel: Path
    sandbox_command: str
    probes: tuple[QualificationProbe, ...]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _publish(path: Path, value: dict[str, object]) -> None:
    raw = _canonical(value) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _probe(name: str, argv: tuple[str, ...], environment: dict[str, str], cwd: Path,
           session_id: str | None, timeout: int, credential: Path | None = None) -> QualificationProbe:
    credential_record: dict[str, object] | None = None
    if credential is not None:
        info = credential.lstat()
        if (credential.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ClaudeQualificationRefused("qualification credential is unsafe")
        credential_record = {"path": str(credential), "sha256": _digest(credential),
                             "device": info.st_dev, "inode": info.st_ino}
    material = {"name": name, "argv": list(argv), "environment": environment, "cwd": str(cwd),
                "session_id": session_id, "timeout_seconds": timeout,
                "credential": credential_record}
    return QualificationProbe(name, argv, tuple(sorted(environment.items())), str(cwd), session_id,
                              timeout, hashlib.sha256(_canonical(material)).hexdigest(),
                              None if credential_record is None else str(credential_record["path"]),
                              None if credential_record is None else str(credential_record["sha256"]),
                              None if credential_record is None else int(credential_record["device"]),
                              None if credential_record is None else int(credential_record["inode"]))


def _copy_private(source: Path, destination: Path) -> None:
    source_info = source.lstat()
    if (source.is_symlink() or not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != os.getuid()
            or source_info.st_nlink != 1 or stat.S_IMODE(source_info.st_mode) != 0o600):
        raise ClaudeQualificationRefused("runtime credential is unsafe")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(source.read_bytes())


def _write_probe_settings(source: Path, destination: Path, denied_credentials: list[str]) -> None:
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
        filesystem = value["sandbox"]["filesystem"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ClaudeQualificationRefused("runtime sandbox settings are malformed") from error
    if not isinstance(filesystem, dict):
        raise ClaudeQualificationRefused("runtime sandbox filesystem settings are malformed")
    filesystem["denyRead"] = denied_credentials
    filesystem["denyWrite"] = denied_credentials
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, separators=(",", ":"))
        output.write("\n")


def prepare_claude_qualification_plan(runtime: Path, binary: Path, workspace: Path,
                                      output: Path, *, version: str, model: str,
                                      effort: str | None, sandbox: str = "workspace-write",
                                      gsd_environment: object = None) -> QualificationPlan:
    """Prepare four immutable probe requests without starting any process."""
    runtime, binary, workspace, output = map(Path, (runtime, binary, workspace, output))
    if output.exists() or output.parent != runtime:
        raise ClaudeQualificationRefused("qualification output must be a new runtime-owned file")
    if version != SUPPORTED_CLAUDE_VERSION or sandbox != "workspace-write":
        raise ClaudeQualificationRefused("unsupported Claude qualification tuple")
    stage = runtime / STAGE_MANIFEST_NAME
    try:
        stage_value = json.loads(stage.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ClaudeQualificationRefused("private runtime stage evidence is unavailable") from error
    if not isinstance(stage_value, dict) or stage_value.get("schema") != STAGE_SCHEMA:
        raise ClaudeQualificationRefused("private runtime stage evidence is invalid")
    temporary = runtime / ".ffs-claude-qualification-tmp"
    negative = runtime / ".ffs-claude-noauth"
    for directory in (temporary, negative):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    # The negative profile intentionally has settings but no credential.  A
    # successful auth status would prove ambient HOME/keychain fallback.
    settings = runtime / "settings.json"
    negative_settings = negative / "settings.json"
    negative_settings.write_bytes(settings.read_bytes())
    negative_settings.chmod(0o600)
    base = claude_closed_environment(runtime, temporary, binary, gsd_environment)
    negative_env = dict(base)
    negative_env["HOME"] = str(negative.parent)
    negative_env["CLAUDE_CONFIG_DIR"] = str(negative)
    common = ClaudeHostAdapter._argv
    sessions = [str(uuid.uuid4()) for _ in range(3)]
    inside, outside = workspace / ".ffs-claude-inside", workspace.parent / (".ffs-claude-outside-" + uuid.uuid4().hex)
    sandbox_script = (
        "printf FFS_INSIDE > " + shlex.quote(str(inside)) + "; "
        "printf FFS_OUTSIDE > " + shlex.quote(str(outside)) + "; write_rc=$?; "
        "/usr/bin/curl --connect-timeout 2 --max-time 4 -fsS https://example.invalid/ >/dev/null 2>&1; "
        "network_rc=$?; printf 'FFS_WRITE_RC=%s FFS_NETWORK_RC=%s\\n' \"$write_rc\" \"$network_rc\""
    )
    sandbox_command = "/bin/sh -c " + shlex.quote(sandbox_script)
    prompts = (
        "Reply with exactly the text FFS_CLAUDE_SESSION_OK and use no tools.",
        "Use Bash exactly once with this exact command, then stop: " + sandbox_command,
        "Use Bash exactly once with this exact command, then stop: "
        + shlex.quote(str(binary.resolve())) + " auth status --json",
    )
    probes = [_probe("auth-negative", (str(binary.resolve()), "auth", "status", "--json"),
                     negative_env, workspace, None, 30)]
    profiles = {name: runtime / (".ffs-claude-probe-" + name) for name in QUALIFICATION_PROBES[1:]}
    denied_credentials = [str(runtime / ".credentials.json")] + [
        str(profile / ".credentials.json") for profile in profiles.values()
    ]
    for name, session, prompt, timeout in zip(QUALIFICATION_PROBES[1:], sessions, prompts, (60, 90, 60)):
        profile = profiles[name]
        profile.mkdir(mode=0o700)
        profile.chmod(0o700)
        profile_settings = profile / "settings.json"
        _write_probe_settings(settings, profile_settings, denied_credentials)
        profile_credential = profile / ".credentials.json"
        _copy_private(runtime / ".credentials.json", profile_credential)
        profile_tmp = profile / "tmp"
        profile_tmp.mkdir(mode=0o700)
        probe_environment = claude_closed_environment(profile, profile_tmp, binary, gsd_environment)
        probes.append(_probe(name, common(binary, workspace, profile_settings, model, effort, session, prompt),
                             probe_environment, workspace, session, timeout, profile_credential))
    stage_sha256 = _digest(stage)
    envelope = hashlib.sha256(_canonical({
        "runtime": str(runtime.resolve()), "stage_sha256": stage_sha256,
        "binary": _binary_chain(binary), "workspace": str(workspace.resolve()),
        "version": version, "model": model, "effort": effort, "sandbox": sandbox,
        "probe_contracts": [probe.contract_sha256 for probe in probes],
    })).hexdigest()
    return QualificationPlan(runtime.resolve(), binary.resolve(), workspace.resolve(), version, model, effort,
                             sandbox, stage_sha256, claude_environment_policy_hash(base), envelope, output,
                             outside, inside, sandbox_command, tuple(probes))


def _auth_negative(result: QualificationResult) -> bool:
    # Claude auth status may return either nonzero or a JSON loggedIn=false
    # record.  Any affirmative login proves ambient credential fallback.
    try:
        value = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        value = {}
    if isinstance(value, dict) and value.get("loggedIn") is True:
        return False
    return result.exit_code != 0 or (isinstance(value, dict) and value.get("loggedIn") is False)


def _nested_auth_denied(stream: str) -> bool:
    compact = stream.replace(" ", "").lower()
    return ('\\"loggedin\\":false' in compact or '"loggedin":false' in compact) and not (
        '\\"loggedin\\":true' in compact or '"loggedin":true' in compact
    )


def _stream_strings(stream: str) -> list[str]:
    values: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    for line in stream.splitlines():
        try:
            visit(json.loads(line))
        except (json.JSONDecodeError, RecursionError):
            continue
    return values


def publish_claude_qualification_results(plan: QualificationPlan,
                                         results: tuple[QualificationResult, ...]) -> QualifiedClaudeRuntime:
    """Verify all fixed results, publish non-secret evidence, and admit runtime."""
    if not isinstance(plan, QualificationPlan) or len(results) != len(QUALIFICATION_PROBES):
        raise ClaudeQualificationRefused("qualification result set is incomplete")
    if any(type(item) is not QualificationResult for item in results):
        raise ClaudeQualificationRefused("qualification result is untyped")
    by_name = {item.name: item for item in results}
    if tuple(item.name for item in results) != QUALIFICATION_PROBES or len(by_name) != len(results):
        raise ClaudeQualificationRefused("qualification result order or identity changed")
    if _digest(plan.runtime / STAGE_MANIFEST_NAME) != plan.stage_sha256:
        raise ClaudeQualificationRefused("runtime stage changed during qualification")
    for probe in plan.probes[1:]:
        if probe.credential_path is None or os.path.lexists(probe.credential_path):
            raise ClaudeQualificationRefused("qualification credential was not consumed")
    execution_credential = plan.runtime / ".credentials.json"
    try:
        credential_info = execution_credential.lstat()
    except OSError as error:
        raise ClaudeQualificationRefused("execution credential is unavailable after qualification") from error
    if (execution_credential.is_symlink() or not stat.S_ISREG(credential_info.st_mode)
            or credential_info.st_uid != os.getuid() or credential_info.st_nlink != 1
            or stat.S_IMODE(credential_info.st_mode) != 0o600):
        raise ClaudeQualificationRefused("execution credential is unsafe after qualification")
    if not _auth_negative(by_name["auth-negative"]):
        raise ClaudeQualificationRefused("ambient Claude subscription authentication is reachable")
    telemetry = {}
    for probe in plan.probes[1:]:
        result = by_name[probe.name]
        if type(result.exit_code) is not int or result.exit_code != 0 or not isinstance(result.stdout, str):
            raise ClaudeQualificationRefused(f"Claude probe failed: {probe.name}")
        try:
            telemetry[probe.name] = parse_claude_telemetry(
                result.stdout.encode(), requested_model=plan.model,
                expected_session_id=str(probe.session_id), expected_version=plan.version,
            )
        except ClaudeHostRefused as error:
            raise ClaudeQualificationRefused(f"Claude probe telemetry failed: {probe.name}") from error
    if not plan.inside_sentinel.is_file() or plan.inside_sentinel.read_text(encoding="utf-8") != "FFS_INSIDE":
        raise ClaudeQualificationRefused("Claude sandbox did not preserve the workspace write")
    if os.path.lexists(plan.outside_sentinel):
        raise ClaudeQualificationRefused("Claude sandbox allowed an outside-workspace write")
    hook_events = set(telemetry["sandbox-hooks"].hook_events)
    hook_families = {event.split(":", 1)[0] for event in hook_events}
    if not {"PreToolUse", "PostToolUse"}.issubset(hook_families):
        raise ClaudeQualificationRefused("Claude hook lifecycle was not observed")
    sandbox_stream = by_name["sandbox-hooks"].stdout
    sandbox_text = "\n".join(_stream_strings(sandbox_stream))
    if (plan.sandbox_command not in sandbox_text
            or re.search(r"FFS_WRITE_RC=[1-9][0-9]*", sandbox_text) is None
            or re.search(r"FFS_NETWORK_RC=[1-9][0-9]*", sandbox_text) is None):
        raise ClaudeQualificationRefused("Claude sandbox denial telemetry is incomplete")
    if not _nested_auth_denied(by_name["nested-auth"].stdout):
        raise ClaudeQualificationRefused("nested Claude retained subscription authentication")
    binary = _binary_chain(plan.binary)
    workspace_info, runtime_info = plan.workspace.stat(), plan.runtime.stat()
    observation = {
        "schema": QUALIFICATION_SCHEMA, "version": plan.version,
        "environment_sha256": plan.environment_sha256,
        "envelope_sha256": plan.envelope_sha256,
        "probe_contracts": {probe.name: probe.contract_sha256 for probe in plan.probes},
        "stream_sha256": {name: value.sha256 for name, value in telemetry.items()},
        "auth_negative": True, "nested_auth_denied": True,
        "hook_events": sorted(hook_events), "sandbox_write_boundary": True,
    }
    evidence = {
        **observation,
        "stderr_sha256": {item.name: hashlib.sha256(item.stderr.encode()).hexdigest() for item in results},
        "exit_codes": {item.name: item.exit_code for item in results},
    }
    _publish(plan.output, evidence)
    qualified = QualifiedClaudeRuntime(
        binary=tuple(sorted(binary.items())),
        runtime=tuple(sorted({"path": str(plan.runtime), "device": runtime_info.st_dev,
                              "inode": runtime_info.st_ino, "settings_sha256": _digest(plan.runtime / "settings.json"),
                              "stage_sha256": plan.stage_sha256}.items())),
        workspace=tuple(sorted({"path": str(plan.workspace), "device": workspace_info.st_dev,
                                "inode": workspace_info.st_ino}.items())),
        supervisor=tuple(sorted(current_supervisor_identity().items())),
        execution=tuple(sorted({"model": plan.model, "effort": plan.effort, "sandbox": plan.sandbox,
                                "network_enabled": False, "roots": [str(plan.workspace)],
                                "tools": _QUALIFIED_TOOLS}.items())),
        observation=tuple(sorted({**observation, "evidence_sha256": _digest(plan.output)}.items())),
    )
    return qualified
