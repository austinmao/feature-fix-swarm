#!/usr/bin/env python3
"""Produce fail-closed, hash-bound evidence from an isolated Codex canary."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
from typing import NamedTuple

SCHEMA = "ffs.codex-runtime-observation/v2"
HOOKS = {"SessionStart"}
DISABLED_FEATURES = (
    "multi_agent", "multi_agent_v2", "plugins", "remote_plugin",
    "recommended_plugins", "plugin_sharing", "apps",
)
SHELL_PROBE_NAME = ".ffs-observer-shell-probe.py"
SHELL_PROBE_COMMAND = 'python3 "$CODEX_HOME/' + SHELL_PROBE_NAME + '"'
SHELL_PROBE_SOURCE = """from pathlib import Path
import json
import os

environment = os.environ
nonce = environment["FFS_HOOK_NONCE"]
admission = Path(environment["FFS_SUPERVISED_ADMISSION_FILE"])
workspace = Path(json.loads(admission.read_text(encoding="utf-8"))["workspace"])
(workspace / f"ffs-observer-allowed-{nonce}.txt").write_text("allowed", encoding="utf-8")
Path(f"{admission}.blocked-{nonce}").write_text("blocked", encoding="utf-8")
"""

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def tree_sha(root: Path, excluded_top: frozenset[str] = frozenset()) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"unsafe or absent tree: {root}")
    for name in excluded_top:
        excluded = root / name
        if os.path.lexists(excluded):
            # Exclusion affects identity only.  Generated content must still
            # be a closed regular tree before it can be observed or reused.
            tree_sha(excluded)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in excluded_top:
            continue
        if path.is_symlink():
            raise ValueError(f"unsafe runtime tree member: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unsafe runtime tree member: {path}")
        name, content = relative.as_posix().encode(), path.read_bytes()
        digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        digest.update(len(content).to_bytes(8, "big")); digest.update(content)
    return digest.hexdigest()

def executable_chain(binary: Path) -> dict[str, str]:
    launcher = binary.resolve()
    if not launcher.is_file() or launcher.is_symlink():
        raise ValueError("Codex launcher is not a regular executable")
    chain = {"launcher_sha256": sha(launcher)}
    is_js = launcher.suffix == ".js"
    if is_js:
        node = Path(os.environ.get("CODEX_NODE_BINARY") or shutil.which("node") or "").resolve()
        if not node.is_file() or node.is_symlink():
            raise ValueError("Codex JS launcher has no resolved regular Node binary")
        chain["node_sha256"] = sha(node.resolve())
    candidates = [Path(os.environ["CODEX_NATIVE_BINARY"])] if os.environ.get("CODEX_NATIVE_BINARY") else []
    # Homebrew/npm launchers live at @openai/codex/bin/codex.js while the
    # platform binary is its @openai/codex-*/vendor sibling.
    if len(launcher.parents) >= 3:
        candidates.extend(launcher.parents[2].glob("codex-*/vendor/*/bin/codex"))
    if len(launcher.parents) >= 2:
        candidates.extend(launcher.parents[1].glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            chain["native_sha256"] = sha(candidate.resolve()); break
    if is_js and "native_sha256" not in chain:
        raise ValueError("Codex JS launcher has no resolved native CLI")
    return chain

def runtime_hashes(runtime: Path) -> dict[str, str]:
    info = runtime.stat()
    return {"path": str(runtime.resolve()), "device": info.st_dev, "inode": info.st_ino,
            "config_sha256": sha(runtime / "config.toml"), "hooks_sha256": sha(runtime / "hooks.json"),
            # Codex materializes its version-owned built-in skills under
            # skills/.system on first startup.  The executable-chain hashes
            # bind that generated surface; excluding it keeps the immutable
            # staged identity stable across the first probe.  The generated
            # tree is still validated and recorded as observation evidence.
            "skills_sha256": tree_sha(runtime / "skills", frozenset({".system"})),
            "agents_sha256": tree_sha(runtime / "agents"),
            "gsd_core_sha256": tree_sha(runtime / "gsd-core"),
            "scripts_sha256": tree_sha(runtime / "scripts"),
            "gsd_manifest_sha256": sha(runtime / "gsd-file-manifest.json")}


def workspace_identity(worktree: Path) -> dict[str, object]:
    info = worktree.stat()
    return {"path": str(worktree.resolve()), "device": info.st_dev, "inode": info.st_ino}

def observer_token(runtime: Path) -> str:
    """Return a repeatable proof token for this staged runtime.

    The observer is itself a staged skill.  A random token in that file made
    an otherwise identical runtime tree hash differently on every launch,
    which is incompatible with resume's immutable-source comparison.  Bind
    the token to the pre-observer runtime instead, deliberately omitting a
    prior observer copy so rerunning the canary is stable too.
    """
    skills = runtime / "skills"
    if not skills.is_dir() or skills.is_symlink():
        raise ValueError(f"unsafe or absent tree: {skills}")
    digest = hashlib.sha256()
    for path in (runtime / "config.toml", runtime / "hooks.json"):
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big")); digest.update(content)
    for path in sorted(skills.rglob("*")):
        relative = path.relative_to(skills)
        if relative.parts[0] in {"ffs-observer", ".system"}:
            continue
        if path.is_symlink():
            raise ValueError(f"unsafe runtime tree member: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"unsafe runtime tree member: {path}")
        name, content = relative.as_posix().encode(), path.read_bytes()
        digest.update(len(name).to_bytes(8, "big")); digest.update(name)
        digest.update(len(content).to_bytes(8, "big")); digest.update(content)
    digest.update(tree_sha(runtime / "agents").encode())
    return f"FFS_OBSERVER_SKILL_{digest.hexdigest()[:32]}"


def prepare_observer_skill(runtime: Path) -> str:
    """Materialize the deterministic observer skill before identity hashing."""
    token = observer_token(runtime)
    skill = runtime / "skills" / "ffs-observer"
    if skill.exists() and (skill.is_symlink() or not skill.is_dir()):
        raise ValueError(f"unsafe observer skill path: {skill}")
    skill.mkdir(mode=0o700, exist_ok=True)
    expected = (
        "---\nname: ffs-observer\ndescription: Controlled runtime-canary skill.\n---\n"
        f"# Observer\nWhen invoked, state this exact token: {token}\n"
    )
    target = skill / "SKILL.md"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f"unsafe observer skill path: {target}")
    if target.exists() and target.read_text(encoding="utf-8") != expected:
        raise ValueError("observer skill identity changed")
    if not target.exists():
        write_private_text(target, expected)
    return token


def _host_capabilities_module():
    here = Path(__file__).resolve()
    # The managed installer keeps scripts/gsd nested and flattens lib files
    # at the managed root; source checkouts retain the root/lib directory.
    candidates = (
        here.parent / "host_capabilities.py",
        here.parents[2] / "host_capabilities.py",
        here.parents[2] / "lib" / "host_capabilities.py",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            import importlib.util
            spec = importlib.util.spec_from_file_location("ffs_host_capabilities_shared", candidate)
            if spec is None or spec.loader is None:
                break
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("shared host_capabilities module is unavailable")

_shared = _host_capabilities_module()
_private_jsonl = _shared._private_jsonl
_thread_id = _shared._thread_id
_session_proof = _shared._session_proof
_native_proof = _shared._native_proof
_native_multi_agent_proof = _shared._native_multi_agent_proof

def _shell_session_proof(runtime: Path, transcript: Path, worktree: Path, model: str, effort: str,
                         program: str, nonce: str, command: str, expect_denial: bool) -> tuple[bool, dict[str, str]]:
    """Require an exact persisted custom-tool result for the ordinary probe."""
    thread_id = _thread_id(transcript)
    if not thread_id:
        return False, {}
    sessions = runtime / "sessions"
    if sessions.is_symlink() or not sessions.is_dir():
        return False, {}
    candidates = [path for path in sessions.rglob(f"rollout-*{thread_id}.jsonl")
                  if path.name.endswith(f"{thread_id}.jsonl") and not path.is_symlink() and path.is_file()]
    if len(candidates) != 1:
        return False, {}
    records = _private_jsonl(candidates[0], allow_readable=True)
    if records is None:
        return False, {}
    expected_cwd = str(worktree.resolve())
    metadata = [entry.get("payload") for entry in records if entry.get("type") == "session_meta"]
    contexts = [entry.get("payload") for entry in records if entry.get("type") == "turn_context"]
    if len(metadata) != 1 or not isinstance(metadata[0], dict) or metadata[0].get("id") != thread_id or metadata[0].get("cwd") != expected_cwd:
        return False, {}
    matching_contexts = [context for context in contexts if isinstance(context, dict)
                         and context.get("cwd") == expected_cwd and context.get("model") == model
                         and context.get("effort") == effort]
    if len(matching_contexts) != 1 or not isinstance(matching_contexts[0].get("turn_id"), str):
        return False, {}
    turn_id = matching_contexts[0]["turn_id"]
    calls, outputs, tool_calls = [], [], []
    for entry in records:
        if entry.get("type") != "response_item" or not isinstance(entry.get("payload"), dict):
            continue
        payload = entry["payload"]
        if payload.get("internal_chat_message_metadata_passthrough", {}).get("turn_id") != turn_id:
            continue
        if payload.get("type") in {"custom_tool_call", "function_call"}:
            tool_calls.append(payload)
        if (payload.get("type") == "custom_tool_call" and payload.get("name") == "exec"
                and payload.get("namespace") in (None, "functions") and payload.get("input") == program):
            calls.append(payload)
        elif payload.get("type") == "custom_tool_call_output":
            outputs.append(payload)
    if len(tool_calls) != 1 or len(calls) != 1 or not isinstance(calls[0].get("call_id"), str):
        return False, {}
    call_id = calls[0]["call_id"]
    matches = [output for output in outputs if output.get("call_id") == call_id]
    if len(matches) != 1:
        return False, {}
    rendered = "\n".join(str(part.get("text", "")) for part in matches[0].get("output", [])
                         if isinstance(part, dict) and part.get("type") == "input_text")
    machine = []
    for line in rendered.splitlines():
        try:
            machine.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    accepted = [item for item in machine if isinstance(item, dict)
                and item.get("nonce") == nonce and item.get("command") == command
                and isinstance(item.get("exit_code"), int)
                and ((item["exit_code"] != 0 and "permissionerror" in str(item.get("output", "")).lower())
                     if expect_denial else item["exit_code"] == 0)]
    if len(accepted) != 1:
        return False, {}
    return True, {"shell_session_sha256": sha(candidates[0]), "shell_thread_id": thread_id,
                  "shell_turn_id": turn_id, "shell_call_id": call_id}


def _shell_exec_program(nonce: str, command: str) -> str:
    return ("const command=" + json.dumps(command)
            + ";const result=await tools.exec_command({cmd:command,yield_time_ms:10000,max_output_tokens:12000});"
            + "text(JSON.stringify({nonce:" + json.dumps(nonce)
            + ",command,exit_code:result.exit_code,output:result.output}))")


def derive(runtime: Path, binary: Path, nonce: str, transcript: Path, hooks: Path, skill_token: str = "",
           native_positive: Path | None = None, native_negative: Path | None = None,
           native_multi_agent: Path | None = None,
           allowed: Path | None = None, blocked: Path | None = None,
           sandbox: str = "workspace-write", worktree: Path | None = None,
           model: str = "", effort: str = "", shell_program: str = "",
           network_enabled: bool = False, roots: list[str] | None = None,
           environment: dict[str, str] | None = None,
           policy_environment: dict[str, str] | None = None,
           observation_created_at_unix: float | None = None) -> dict:
    lines = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
    items = [line.get("item", {}) for line in lines if line.get("type") == "item.completed"]
    commands = [item for item in items if item.get("type") == "command_execution"]
    messages = "\n".join(str(item.get("text", "")) for item in items if item.get("type") == "agent_message")
    events = [line.split(" ", 1)[1] for line in hooks.read_text(encoding="utf-8").splitlines() if line.startswith(nonce + " ") and " " in line]
    def is_probe_command(item: dict) -> bool:
        command = str(item.get("command", ""))
        # Real probes bind the complete generated program; the compatibility
        # branch retains the older direct derive fixture only when no program
        # was supplied by the caller.
        return (command == shell_program if shell_program else
                (str(allowed) in command or (allowed is not None and allowed.name in command))
                and str(blocked) in command)
    denied_by_command = any(item.get("exit_code") not in (None, 0)
                            and "permissionerror" in str(item.get("aggregated_output", "")).lower()
                            and is_probe_command(item) for item in commands)
    successful_write = any(item.get("exit_code") == 0 and is_probe_command(item) for item in commands)
    session_bound, shell_artifacts = (False, {})
    if shell_program:
        expected_command = ""
        marker = "const command="
        if shell_program.startswith(marker):
            try:
                candidate, _end = json.JSONDecoder().raw_decode(shell_program[len(marker):])
                if isinstance(candidate, str):
                    expected_command = candidate
            except json.JSONDecodeError:
                pass
        expected_program = _shell_exec_program(nonce, expected_command) if expected_command else ""
        if worktree is not None and shell_program == expected_program:
            session_bound, shell_artifacts = _shell_session_proof(
                runtime, transcript, worktree, model, effort, expected_program, nonce, expected_command,
                sandbox != "danger-full-access")
        # The persisted custom tool call is the proof for new probes.  A
        # command_execution event is only a compatibility format for direct
        # derive callers that did not request the canonical program.
        denied_by_command = session_bound
        successful_write = session_bound and allowed is not None and allowed.is_file()
    denied = denied_by_command
    allowed_exists = allowed is not None and allowed.is_file()
    blocked_exists = blocked is not None and blocked.exists()
    boundary = {
        "workspace-write": denied and allowed_exists and not blocked_exists,
        "read-only": denied and not allowed_exists and not blocked_exists,
        "danger-full-access": successful_write and allowed_exists and blocked_exists,
    }.get(sandbox, False)
    native_denied, native_artifacts = _native_proof(
        native_positive, native_negative, runtime=runtime, nonce=nonce,
        worktree=worktree, model=model, effort=effort,
    )
    multi_agent_denied, multi_agent_artifacts = _native_multi_agent_proof(
        native_multi_agent, runtime=runtime, nonce=nonce,
        worktree=worktree, model=model, effort=effort,
    )
    roots = [str(Path(root).resolve()) for root in (roots or ([str(worktree)] if worktree is not None else []))]
    closed_environment = environment or {}
    if policy_environment is not None:
        environment_sha256 = _shared.codex_environment_policy_hash(policy_environment)
    elif set(closed_environment) == {"HOME", "CODEX_HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "NO_COLOR"}:
        environment_sha256 = _shared.codex_environment_policy_hash(closed_environment)
    else:
        # Direct derive callers from the original evidence format do not have
        # a production envelope.  Keep their bounded compatibility hash;
        # run_canary always supplies policy_environment below.
        environment_sha256 = _shared.closed_environment_hash(closed_environment)
    created_at = time.time() if observation_created_at_unix is None else observation_created_at_unix
    if (isinstance(created_at, bool) or not isinstance(created_at, (int, float))
            or not float("-inf") < float(created_at) < float("inf")):
        raise ValueError("invalid observation timestamp")
    system_skills = runtime / "skills" / ".system"
    system_skills_sha256 = tree_sha(system_skills) if system_skills.exists() else None
    record = {"schema": SCHEMA, "binary": executable_chain(binary), "runtime": runtime_hashes(runtime),
            "artifacts": {"transcript_sha256": sha(transcript), "hooks_sha256": sha(hooks), **shell_artifacts, **native_artifacts},
            "observed": {"auth": (any(line.get("type") == "thread.started" for line in lines)
                                  and any(line.get("type") == "turn.completed" for line in lines)
                                  and bool(messages)),
                         "skill_discovery": bool(skill_token) and skill_token in messages,
                         # Retained for consumers that specifically require a
                         # workspace boundary.  It must never be claimed for
                         # an authorized danger-full-access execution.
                         "shell_denied": sandbox != "danger-full-access" and denied and allowed_exists and not blocked_exists,
                         "shell_denial_source": ("persisted-session-paired" if denied_by_command and shell_program
                                                   else "command_execution" if denied_by_command else "unmet"),
                         "sandbox_policy": sandbox, "write_boundary": boundary,
                         "native_network_denied": native_denied,
                         "native_network_proof": "persisted-session-paired" if native_denied else "unmet",
                         "native_multi_agent_denied": multi_agent_denied,
                         "native_multi_agent_proof": "persisted-session-paired" if multi_agent_denied else "unmet",
                         "hooks": HOOKS.issubset(events), "hook_events": events},
            "observation": {"id": nonce, "created_at_unix": created_at,
                            "environment_sha256": environment_sha256,
                            "telemetry_schema": _shared.TELEMETRY_SCHEMA},
            "telemetry": {"schema": _shared.TELEMETRY_SCHEMA, "measurement": "unavailable"},
            "supervisor": _shared.current_supervisor_identity(),
            "workspace": workspace_identity(worktree) if worktree is not None else None,
            "execution": {"model": model, "effort": effort, "sandbox": sandbox,
                          "network_enabled": network_enabled, "roots": roots,
                          "disabled_features": list(DISABLED_FEATURES)}}
    record["artifacts"].update(multi_agent_artifacts)
    shell_probe = runtime / SHELL_PROBE_NAME
    if shell_program and shell_probe.is_file() and not shell_probe.is_symlink():
        record["artifacts"]["shell_probe_sha256"] = sha(shell_probe)
    if system_skills_sha256 is not None:
        record["artifacts"]["codex_system_skills_sha256"] = system_skills_sha256
    return record

def write_private(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(record, out, sort_keys=True); out.write("\n")


def write_private_text(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)


def prepare_shell_probe(runtime: Path) -> Path:
    """Materialize a stable target so the model never transcribes host paths."""
    target = runtime / SHELL_PROBE_NAME
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f"unsafe shell probe path: {target}")
    if target.exists() and target.read_text(encoding="utf-8") != SHELL_PROBE_SOURCE:
        raise ValueError("shell probe identity changed")
    if not target.exists():
        write_private_text(target, SHELL_PROBE_SOURCE)
    return target


def write_invocation(path: Path, argv: list[str], result: subprocess.CompletedProcess[str]) -> None:
    """Retain bounded private CLI diagnostics without copying its environment."""
    stderr = result.stderr
    limit = 64 * 1024
    if len(stderr.encode("utf-8", errors="replace")) > limit:
        stderr = stderr.encode("utf-8", errors="replace")[-limit:].decode("utf-8", errors="replace")
    write_private(path, {"argv": argv, "exit_code": result.returncode, "stderr": stderr,
                         "stderr_truncated": len(result.stderr.encode("utf-8", errors="replace")) > limit})

QUALIFICATION_PROBES = ("ordinary", "native-positive", "native-negative", "native-multi-agent")
MAX_PROBE_OUTPUT_BYTES = 16 * 1024 * 1024


class QualificationProbe(NamedTuple):
    name: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    timeout_seconds: int
    transcript: Path
    invocation: Path


class QualificationSeed(NamedTuple):
    nonce: str
    skill_token: str
    observation_created_at_unix: float


class QualificationPlan(NamedTuple):
    runtime: Path
    binary: Path
    worktree: Path
    output: Path
    nonce: str
    hooks: Path
    skill_token: str
    allowed: Path
    blocked: Path
    sandbox: str
    model: str
    effort: str
    shell_program: str
    network_enabled: bool
    roots: tuple[str, ...]
    policy_environment: tuple[tuple[str, str], ...]
    observation_created_at_unix: float
    runtime_identity: str
    binary_identity: str
    workspace_identity: str
    probes: tuple[QualificationProbe, ...]
    # (st_dev, st_ino) of the probe TMPDIR when this plan created it; None if it pre-existed.
    scratch_identity: tuple[int, int] | None = None


class QualificationResult(NamedTuple):
    name: str
    stdout: str
    stderr: str
    exit_code: int


def prepare_qualification_seed(runtime: Path) -> QualificationSeed:
    """Freeze every receipt field which is otherwise created during planning."""
    runtime = Path(runtime).absolute()
    return QualificationSeed(os.urandom(16).hex(), prepare_observer_skill(runtime), time.time())


def preview_qualification_runtime(
    seed: QualificationSeed, runtime: Path, binary: Path, worktree: Path, *,
    model: str = "", effort: str = "", sandbox: str = "workspace-write",
    network_enabled: bool = False, roots: list[str] | None = None,
    gsd_environment: object = None,
):
    """Compute the future receipt while its immutable admission file is absent."""
    if not isinstance(seed, QualificationSeed):
        raise ValueError("invalid qualification seed")
    runtime, binary, worktree = (Path(value).absolute() for value in (runtime, binary, worktree))
    if prepare_observer_skill(runtime) != seed.skill_token:
        raise ValueError("qualification seed skill identity changed")
    environment = _shared.codex_closed_environment(
        runtime, runtime / ".ffs-codex-policy-tmp", binary, executable_chain(binary),
    )
    if gsd_environment is not None and callable(getattr(gsd_environment, "as_dict", None)):
        environment.update(gsd_environment.as_dict())
        environment_sha256 = _shared.preview_gsd_codex_environment_policy_hash(environment)
    elif gsd_environment is None:
        environment_sha256 = _shared.codex_environment_policy_hash(environment)
    else:
        raise ValueError("invalid GSD supervisor environment")
    root_values = tuple(str(Path(root).resolve()) for root in (roots or [str(worktree)]))
    observation = {
        "id": seed.nonce, "created_at_unix": seed.observation_created_at_unix,
        "environment_sha256": environment_sha256,
        "telemetry_schema": _shared.TELEMETRY_SCHEMA,
    }
    execution = {
        "model": model, "effort": effort, "sandbox": sandbox,
        "network_enabled": network_enabled, "roots": list(root_values),
        "disabled_features": list(DISABLED_FEATURES),
    }
    return _shared.QualifiedCodexRuntime(
        binary=tuple(sorted(executable_chain(binary).items())),
        runtime=tuple(sorted(runtime_hashes(runtime).items())),
        workspace=tuple(sorted(workspace_identity(worktree).items())),
        supervisor=tuple(sorted(_shared.current_supervisor_identity().items())),
        execution=tuple(sorted(execution.items())),
        observation=tuple(sorted(observation.items())),
    )


def prepare_qualification_plan(runtime: Path, binary: Path, worktree: Path, output: Path, timeout: int, model: str = "", effort: str = "", sandbox: str = "workspace-write",
               network_enabled: bool = False, roots: list[str] | None = None,
                               gsd_environment: object = None,
                               seed: QualificationSeed | None = None,
                               preview: bool = False,
                               allow_existing_evidence: bool = False) -> QualificationPlan:
    """Stage four fixed probes for an owner; never launch a process.

    The owner must admit each probe through its normal dispatch boundary.
    This descriptor is not admission authority or a capability receipt.
    """
    if type(timeout) is not int or timeout < 1:
        raise ValueError("qualification timeout must be a positive integer")
    if type(network_enabled) is not bool:
        raise ValueError("qualification network policy must be boolean")
    if any(not isinstance(value, str) or "\x00" in value for value in (model, effort)):
        raise ValueError("qualification model and effort must be strings")
    runtime, binary, worktree, output = (Path(value).absolute() for value in (runtime, binary, worktree, output))
    if any(path.is_symlink() for path in (runtime, worktree, output)):
        raise ValueError("qualification paths must not be symlinks")
    if output.exists() and not allow_existing_evidence:
        raise ValueError("qualification output already exists")
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError("unsupported qualification sandbox")
    seed = prepare_qualification_seed(runtime) if seed is None else seed
    if (not isinstance(seed, QualificationSeed)
            or not isinstance(seed.nonce, str) or len(seed.nonce) != 32
            or any(char not in "0123456789abcdef" for char in seed.nonce)
            or isinstance(seed.observation_created_at_unix, bool)
            or not isinstance(seed.observation_created_at_unix, (int, float))
            or prepare_observer_skill(runtime) != seed.skill_token):
        raise ValueError("invalid qualification seed")
    nonce = seed.nonce; hooks = runtime / "observer-hooks.log"
    # Keep the exact CLI transcript with the private evidence bundle.  A
    # system-temporary name was impossible to audit from the record alone.
    transcript = runtime / f"observer-transcript-{nonce}.jsonl"
    token = seed.skill_token
    prepare_shell_probe(runtime)
    allowed = worktree / f"ffs-observer-allowed-{nonce}.txt"
    tmpdir = worktree / ".ffs-observer-tmp"
    try:
        tmpdir.mkdir()
    except FileExistsError:
        scratch_identity = None
    else:
        scratch_info = tmpdir.lstat()
        scratch_identity = (scratch_info.st_dev, scratch_info.st_ino)
    if preview and gsd_environment is not None:
        if not callable(getattr(gsd_environment, "as_dict", None)):
            raise ValueError("invalid GSD supervisor environment")
        production_environment = _shared.codex_closed_environment(
            runtime, runtime / ".ffs-codex-policy-tmp", binary, executable_chain(binary),
        )
        production_environment.update(gsd_environment.as_dict())
        _shared.preview_gsd_codex_environment_policy_hash(production_environment)
    else:
        exact_gsd_environment = (
            gsd_environment.as_dict()
            if gsd_environment is not None and callable(getattr(gsd_environment, "as_dict", None))
            else gsd_environment
        )
        production_environment = _shared.codex_closed_environment(
            runtime, runtime / ".ffs-codex-policy-tmp", binary, executable_chain(binary),
            exact_gsd_environment,
        )
    admission_file = production_environment.get("FFS_SUPERVISED_ADMISSION_FILE")
    blocked = (Path(admission_file + f".blocked-{nonce}") if isinstance(admission_file, str)
               else Path(f"/var/empty/ffs-observer-blocked-{nonce}"))
    shell_command = SHELL_PROBE_COMMAND
    shell_exec_program = _shell_exec_program(nonce, shell_command)
    prompt = (f"Invoke $ffs-observer and state its exact token. Then call functions.exec exactly once with the following program; "
              "do not simulate, quote, or describe its result instead of calling it. "
              f"Report whatever the tool returns. Do not use native web tools.\n{shell_exec_program}")
    env = {**production_environment, "TMPDIR": str(tmpdir),
           "FFS_HOOK_OBSERVATION": str(hooks), "FFS_HOOK_NONCE": nonce}
    disabled_argv = [item for feature in DISABLED_FEATURES for item in ("--disable", feature)]
    sandbox_argv = [
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    ]
    command = [str(binary), "exec", "--json", "--strict-config", "--ignore-user-config", "--ignore-rules",
               "--dangerously-bypass-hook-trust", "--sandbox", sandbox, *sandbox_argv, *disabled_argv,
               "--cd", str(worktree), prompt]
    if model:
        command[3:3] = ["-c", 'model=' + json.dumps(model)]
    if effort:
        command[3:3] = ["-c", 'model_reasoning_effort=' + json.dumps(effort)]
    positive, negative = runtime / f"native-positive-{nonce}.jsonl", runtime / f"native-negative-{nonce}.jsonl"
    multi_agent = runtime / f"native-multi-agent-{nonce}.jsonl"
    positive_program = f"text({{nonce:{json.dumps(nonce)},available:typeof tools.web__run==='function'}})"
    negative_program = f"text({{nonce:{json.dumps(nonce)},attempt:true}});await tools.web__run({{time:[{{utc_offset:'+00:00'}}]}})"
    multi_agent_program = (f"text({{nonce:{json.dumps(nonce)},multi_agent:typeof tools.multi_agent==='function',"
                           "multi_agent_v2:typeof tools.multi_agent_v2==='function'})")
    native_prompt = "Call functions.exec exactly once with the following program and do not use any other tool: "
    # Every probe persists its own private session.  The native probes require
    # call/output linkage; the ordinary probe additionally needs a session
    # context because some versions omit shell-tool output from rollouts.
    positive_command = command[:-1].copy()
    positive_command[3:3] = ["-c", 'web_search="live"']
    positive_command.append(native_prompt + positive_program)
    negative_command = [argument for argument in command[:-1] if argument != "--ephemeral"]
    negative_command[3:3] = ["-c", 'web_search="disabled"']
    negative_command.append(native_prompt + negative_program)
    multi_agent_command = command[:-1].copy()
    multi_agent_command.append(native_prompt + multi_agent_program)
    probe_commands = (command, positive_command, negative_command, multi_agent_command)
    transcripts = (transcript, positive, negative, multi_agent)
    probes = tuple(QualificationProbe(
        name, tuple(argv), tuple(sorted(env.items())), min(timeout, maximum), transcript_path,
        runtime / f"observer-{name}-invocation-{nonce}.json",
    ) for name, argv, transcript_path, maximum in zip(
        QUALIFICATION_PROBES, probe_commands, transcripts, (45, 60, 45, 45)))
    return QualificationPlan(
        runtime, binary, worktree, output, nonce, hooks, token, allowed, blocked, sandbox,
        model, effort, shell_exec_program, network_enabled,
        tuple(str(Path(root).resolve()) for root in (roots or [str(worktree)])),
        tuple(sorted(production_environment.items())), seed.observation_created_at_unix,
        json.dumps(runtime_hashes(runtime), sort_keys=True),
        json.dumps(executable_chain(binary), sort_keys=True),
        json.dumps(workspace_identity(worktree), sort_keys=True), probes, scratch_identity,
    )


def preview_qualified_runtime(plan: QualificationPlan):
    """Compute the exact receipt identity before publishing probe evidence.

    Qualification freezes its observation timestamp in the plan.  Everything
    else in the receipt is already a captured plan identity, so the supervisor
    can publish its immutable admission descriptor before the first probe.
    """
    if not isinstance(plan, QualificationPlan):
        raise ValueError("invalid qualification plan")
    try:
        runtime = json.loads(plan.runtime_identity)
        binary = json.loads(plan.binary_identity)
        workspace = json.loads(plan.workspace_identity)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid qualification plan identity") from exc
    environment = dict(plan.policy_environment)
    admission = environment.get("FFS_SUPERVISED_ADMISSION_FILE")
    environment_sha256 = (
        _shared.preview_gsd_codex_environment_policy_hash(environment)
        if isinstance(admission, str) and not os.path.lexists(admission)
        else _shared.codex_environment_policy_hash(environment)
    )
    observation = {
        "id": plan.nonce,
        "created_at_unix": plan.observation_created_at_unix,
        "environment_sha256": environment_sha256,
        "telemetry_schema": _shared.TELEMETRY_SCHEMA,
    }
    execution = {
        "model": plan.model, "effort": plan.effort, "sandbox": plan.sandbox,
        "network_enabled": plan.network_enabled, "roots": list(plan.roots),
        "disabled_features": list(DISABLED_FEATURES),
    }
    return _shared.QualifiedCodexRuntime(
        binary=tuple(sorted(binary.items())), runtime=tuple(sorted(runtime.items())),
        workspace=tuple(sorted(workspace.items())),
        supervisor=tuple(sorted(_shared.current_supervisor_identity().items())),
        execution=tuple(sorted(execution.items())),
        observation=tuple(sorted(observation.items())),
    )


def publish_qualification_results(plan: QualificationPlan,
                                  results: tuple[QualificationResult, ...]) -> dict:
    """Derive private observation evidence from owner-supplied completions.

    All four completed results are required before any transcript is written.
    Existing persisted-session/native/hook validators remain authoritative;
    valid CLI output alone never establishes a successful qualification.
    """
    if not isinstance(plan, QualificationPlan) or tuple(probe.name for probe in plan.probes) != QUALIFICATION_PROBES:
        raise ValueError("invalid qualification plan")
    if not isinstance(results, (tuple, list)) or len(results) != len(QUALIFICATION_PROBES):
        raise ValueError("four qualification results are required")
    by_name = {}
    for result in results:
        if not isinstance(result, QualificationResult) or result.name not in QUALIFICATION_PROBES or result.name in by_name:
            raise ValueError("invalid or duplicate qualification result")
        if (type(result.exit_code) is not int or not -255 <= result.exit_code <= 255
                or not isinstance(result.stdout, str) or not isinstance(result.stderr, str)):
            raise ValueError("malformed qualification completion")
        if any(len(value.encode("utf-8")) > MAX_PROBE_OUTPUT_BYTES for value in (result.stdout, result.stderr)):
            raise ValueError("qualification completion exceeds output bound")
        try:
            records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        except (ValueError, TypeError) as exc:
            raise ValueError("malformed qualification transcript") from exc
        if not records or any(not isinstance(record, dict) or not isinstance(record.get("type"), str)
                              for record in records):
            raise ValueError("malformed qualification transcript")
        if any(record["type"] == "item.completed" and (not isinstance(record.get("item"), dict)
                or not isinstance(record["item"].get("type"), str)) for record in records):
            raise ValueError("malformed qualification item")
        # Failed commands retain their diagnostic streams but cannot manufacture
        # a completed turn. Success must include both machine-originated events.
        if result.exit_code == 0 and not {"thread.started", "turn.completed"}.issubset(
                {record["type"] for record in records}):
            raise ValueError("incomplete qualification transcript")
        by_name[result.name] = result
    if (json.dumps(runtime_hashes(plan.runtime), sort_keys=True) != plan.runtime_identity
            or json.dumps(executable_chain(plan.binary), sort_keys=True) != plan.binary_identity
            or json.dumps(workspace_identity(plan.worktree), sort_keys=True) != plan.workspace_identity):
        raise ValueError("qualification source identity changed")
    paths = [plan.output, *(probe.transcript for probe in plan.probes),
             *(probe.invocation for probe in plan.probes)]
    if len(set(paths)) != len(paths) or any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError("qualification evidence already exists")
    if plan.hooks.is_symlink() or (plan.hooks.exists() and not plan.hooks.is_file()):
        raise ValueError("unsafe qualification hooks")
    for probe in plan.probes:
        result = by_name[probe.name]
        write_private_text(probe.transcript, result.stdout)
        completed = subprocess.CompletedProcess(probe.argv, result.exit_code, result.stdout, result.stderr)
        write_invocation(probe.invocation, list(probe.argv), completed)
    if not plan.hooks.exists():
        write_private_text(plan.hooks, "")
    ordinary, positive, negative, multi_agent = plan.probes
    record = derive(
        plan.runtime, plan.binary, plan.nonce, ordinary.transcript, plan.hooks, plan.skill_token,
        positive.transcript, negative.transcript, multi_agent.transcript, plan.allowed, plan.blocked,
        plan.sandbox, plan.worktree, plan.model, plan.effort, plan.shell_program,
        plan.network_enabled, list(plan.roots), dict(ordinary.environment), dict(plan.policy_environment),
        plan.observation_created_at_unix,
    )
    record["artifacts"].update({
        probe.name.replace("-", "_") + "_invocation_sha256": sha(probe.invocation)
        for probe in plan.probes
    })
    record["exit_code"] = by_name["ordinary"].exit_code
    record["native_exit_codes"] = {
        "positive": by_name["native-positive"].exit_code,
        "negative": by_name["native-negative"].exit_code,
        "multi_agent": by_name["native-multi-agent"].exit_code,
    }
    # Exclusive creation prevents replacing a previous qualification receipt.
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    write_private_text(plan.output, json.dumps(record, sort_keys=True) + "\n")
    plan.allowed.unlink(missing_ok=True)
    plan.blocked.unlink(missing_ok=True)
    return record


def run_canary(runtime: Path, binary: Path, worktree: Path, output: Path, timeout: int, model: str = "", effort: str = "", sandbox: str = "workspace-write",
               network_enabled: bool = False, roots: list[str] | None = None,
               gsd_environment: object = None) -> int:
    """Legacy/test transport wrapper; managed owners dispatch the plan themselves."""
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        write_private(output, {"schema": SCHEMA, "status": "unsupported_probe_sandbox"}); return 78
    plan = prepare_qualification_plan(runtime, binary, worktree, output, timeout, model, effort,
                                      sandbox, network_enabled, roots,
                                      (_shared.gsd_supervisor_environment_from_process()
                                       if gsd_environment is None else gsd_environment))
    results = []
    try:
        for probe in plan.probes:
            result = subprocess.run(list(probe.argv), env=dict(probe.environment), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=probe.timeout_seconds, check=False)
            results.append(QualificationResult(probe.name, result.stdout, result.stderr, result.returncode))
    except subprocess.TimeoutExpired:
        write_private(output, {"schema": SCHEMA, "status": "timeout"}); return 124
    publish_qualification_results(plan, tuple(results))
    return 0

def main() -> int:
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("derive")
    d.add_argument("runtime", type=Path); d.add_argument("binary", type=Path); d.add_argument("nonce"); d.add_argument("transcript", type=Path); d.add_argument("hooks", type=Path); d.add_argument("output", type=Path); d.add_argument("--skill-token", default=""); d.add_argument("--native-positive", type=Path); d.add_argument("--native-negative", type=Path); d.add_argument("--allowed", type=Path); d.add_argument("--blocked", type=Path); d.add_argument("--sandbox", default="workspace-write"); d.add_argument("--worktree", type=Path); d.add_argument("--model", default=""); d.add_argument("--effort", default="")
    r = sub.add_parser("run")
    r.add_argument("runtime", type=Path); r.add_argument("binary", type=Path); r.add_argument("worktree", type=Path); r.add_argument("output", type=Path); r.add_argument("--timeout", type=int, default=180); r.add_argument("--model", default=""); r.add_argument("--effort", default=""); r.add_argument("--sandbox", default="workspace-write"); r.add_argument("--network-enabled", choices=("true", "false"), default="false"); r.add_argument("--root", action="append")
    args = parser.parse_args()
    if args.command == "run": return run_canary(args.runtime, args.binary, args.worktree, args.output, args.timeout, args.model, args.effort, args.sandbox, args.network_enabled == "true", args.root)
    write_private(args.output, derive(args.runtime, args.binary, args.nonce, args.transcript, args.hooks, args.skill_token, args.native_positive, args.native_negative, None, args.allowed, args.blocked, args.sandbox, args.worktree, args.model, args.effort)); return 0

if __name__ == "__main__": raise SystemExit(main())
