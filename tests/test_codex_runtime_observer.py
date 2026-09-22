from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("observer", ROOT / "scripts/gsd/codex-runtime-observer.py")
assert SPEC and SPEC.loader
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"; runtime.mkdir()
    (runtime / "config.toml").write_text("x")
    (runtime / "hooks.json").write_text("{}")
    (runtime / "skills").mkdir(); (runtime / "skills" / "a.md").write_text("skill")
    (runtime / "agents").mkdir(); (runtime / "agents" / "a.toml").write_text("agent")
    (runtime / "gsd-core").mkdir(); (runtime / "gsd-core" / "a.md").write_text("core")
    (runtime / "scripts").mkdir(); (runtime / "scripts" / "a.js").write_text("script")
    (runtime / "gsd-file-manifest.json").write_text("{}")
    binary = tmp_path / "codex"; binary.write_text("#!/bin/sh\n"); binary.chmod(0o700)
    return runtime, binary


def test_derive_requires_real_hook_events_skill_token_and_permission_error(tmp_path: Path):
    runtime, binary = _runtime(tmp_path)
    nonce = "nonce"
    allowed, blocked = tmp_path / "allowed", tmp_path / "blocked"
    allowed.write_text("allowed")
    stream = tmp_path / "stream.jsonl"
    stream.write_text("\n".join(json.dumps(x) for x in [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "TOKEN"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": f"{allowed} {blocked}", "exit_code": 1, "aggregated_output": "PermissionError"}},
        {"type": "turn.completed"},
    ]))
    hooks = tmp_path / "hooks"; hooks.write_text("\n".join(f"{nonce} {event}" for event in observer.HOOKS))
    record = observer.derive(runtime, binary, nonce, stream, hooks, "TOKEN", allowed=allowed, blocked=blocked)
    assert record["observed"]["auth"] is True
    assert record["observed"]["skill_discovery"] is True
    assert record["observed"]["shell_denied"] is True
    assert record["observed"]["hooks"] is True
    assert record["observed"]["native_network_proof"] == "unmet"


def test_runtime_hashes_bind_every_staged_skill_and_agent(tmp_path: Path):
    runtime, _ = _runtime(tmp_path)
    before = observer.runtime_hashes(runtime)
    (runtime / "skills" / "other.md").write_text("changed")
    (runtime / "agents" / "other.toml").write_text("changed")
    after = observer.runtime_hashes(runtime)
    assert before["skills_sha256"] != after["skills_sha256"]
    assert before["agents_sha256"] != after["agents_sha256"]


def test_codex_generated_system_skills_are_separate_from_staged_identity(tmp_path: Path):
    runtime, _ = _runtime(tmp_path)
    before_hashes = observer.runtime_hashes(runtime)
    before_token = observer.observer_token(runtime)
    generated = runtime / "skills" / ".system" / "builtin"
    generated.mkdir(parents=True)
    (generated / "SKILL.md").write_text("binary-owned")
    assert observer.runtime_hashes(runtime) == before_hashes
    assert observer.observer_token(runtime) == before_token
    assert observer.tree_sha(runtime / "skills" / ".system")


def test_codex_generated_system_skills_reject_links(tmp_path: Path):
    runtime, _ = _runtime(tmp_path)
    generated = runtime / "skills" / ".system"
    generated.mkdir()
    (generated / "unsafe").symlink_to(runtime / "config.toml")
    try:
        observer.runtime_hashes(runtime)
    except ValueError as exc:
        assert "unsafe runtime tree member" in str(exc)
    else:
        raise AssertionError("unsafe generated skill link was accepted")


def test_runtime_hashes_bind_gsd_bundle_and_manifest(tmp_path: Path):
    runtime = tmp_path / "runtime"
    for name in ("skills", "agents", "gsd-core", "scripts"):
        (runtime / name).mkdir(parents=True)
        (runtime / name / "owned").write_text(name)
    (runtime / "config.toml").write_text("config")
    (runtime / "hooks.json").write_text("hooks")
    (runtime / "gsd-file-manifest.json").write_text("manifest")
    before = observer.runtime_hashes(runtime)
    (runtime / "gsd-core" / "owned").write_text("changed")
    (runtime / "scripts" / "owned").write_text("changed")
    (runtime / "gsd-file-manifest.json").write_text("changed")
    after = observer.runtime_hashes(runtime)
    assert before["gsd_core_sha256"] != after["gsd_core_sha256"]
    assert before["scripts_sha256"] != after["scripts_sha256"]
    assert before["gsd_manifest_sha256"] != after["gsd_manifest_sha256"]


def test_observation_policy_hash_replays_across_nonce_and_probe_temp_leaf(tmp_path: Path):
    runtime, binary = _runtime(tmp_path)
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps({"type": "thread.started"}))
    hooks = tmp_path / "hooks"
    hooks.write_text("")
    worktree = tmp_path / "work"
    worktree.mkdir()
    base = {
        "HOME": str(runtime), "CODEX_HOME": str(runtime),
        "PATH": f"{binary.parent}:/usr/bin:/bin", "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8", "NO_COLOR": "1",
    }
    first = observer.derive(
        runtime, binary, "a" * 32, stream, hooks, worktree=worktree,
        environment={**base, "TMPDIR": str(worktree / ".probe-a")},
        policy_environment={**base, "TMPDIR": str(runtime / "ffs-codex-a")},
    )
    second = observer.derive(
        runtime, binary, "b" * 32, stream, hooks, worktree=worktree,
        environment={**base, "TMPDIR": str(worktree / ".probe-b")},
        policy_environment={**base, "TMPDIR": str(runtime / "ffs-codex-b")},
    )
    assert first["observation"]["environment_sha256"] == second["observation"]["environment_sha256"]


def test_paired_native_probe_requires_real_positive_and_unavailable_negative(tmp_path: Path):
    runtime, binary = _runtime(tmp_path)
    stream = tmp_path / "stream"; stream.write_text(json.dumps({"type": "thread.started"}))
    hooks = tmp_path / "hooks"; hooks.write_text("")
    positive = tmp_path / "positive"
    positive.write_text("\n".join((json.dumps({"type": "item.completed", "item": {"type": "web_search", "query": "example.com"}}), json.dumps({"type": "turn.completed"}))))
    negative = tmp_path / "negative"
    negative.write_text("\n".join((json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FFS_NATIVE_WEB_UNAVAILABLE"}}), json.dumps({"type": "turn.completed"}))))
    record = observer.derive(runtime, binary, "nonce", stream, hooks, native_positive=positive, native_negative=negative)
    # A prompted marker is agent-authored text, not machine-originated proof
    # that the CLI denied a native tool invocation.
    assert record["observed"]["native_network_denied"] is False
    assert record["observed"]["native_network_proof"] == "unmet"
    negative.write_text(negative.read_text() + "\n" + json.dumps({"type": "item.completed", "item": {"type": "web_search", "query": "example.com"}}))
    assert observer.derive(runtime, binary, "nonce", stream, hooks, native_positive=positive, native_negative=negative)["observed"]["native_network_denied"] is False


def test_timeout_writes_private_unmet_record(tmp_path: Path, monkeypatch):
    runtime, binary = _runtime(tmp_path)
    worktree = tmp_path / "work"; worktree.mkdir()
    output = tmp_path / "observation.json"
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(observer.subprocess, "run", timeout)
    assert observer.run_canary(runtime, binary, worktree, output, 1) == 124
    assert json.loads(output.read_text()) == {"schema": observer.SCHEMA, "status": "timeout"}
    assert output.stat().st_mode & 0o777 == 0o600


def test_canary_disables_both_native_multi_agent_surfaces(tmp_path: Path, monkeypatch):
    runtime, binary = _runtime(tmp_path)
    worktree = tmp_path / "work"; worktree.mkdir()
    output = tmp_path / "observation.json"
    commands = []
    def timeout(command, **kwargs):
        commands.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(observer.subprocess, "run", timeout)
    assert observer.run_canary(runtime, binary, worktree, output, 1) == 124
    assert commands and commands[0].count("--disable") == 7
    assert [commands[0][index + 1] for index, value in enumerate(commands[0]) if value == "--disable"] == [
        "multi_agent", "multi_agent_v2", "plugins", "remote_plugin",
        "recommended_plugins", "plugin_sharing", "apps",
    ]


def test_qualification_shell_probe_uses_hashable_runtime_script_not_copied_paths(tmp_path: Path):
    runtime, binary = _runtime(tmp_path)
    worktree = tmp_path / "work"; worktree.mkdir()
    admission = runtime / "supervisor-admission.json"
    admission.write_text(json.dumps({"workspace": str(worktree)}) + "\n")
    admission.chmod(0o600)
    bridge = ROOT / "lib/run_state/gsd_wave_bridge.py"
    environment = observer._shared.GsdSupervisorEnvironment(
        "ffs-supervised-process", "patches", str(admission),
        json.dumps([str(Path(__import__('sys').executable)), str(bridge)], separators=(",", ":")),
    )
    plan = observer.prepare_qualification_plan(
        runtime, binary, worktree, runtime / "observation.json", 30,
        gsd_environment=environment,
    )
    probe = runtime / observer.SHELL_PROBE_NAME
    assert probe.read_text() == observer.SHELL_PROBE_SOURCE
    assert stat.S_IMODE(probe.stat().st_mode) == 0o600
    prompt = plan.probes[0].argv[-1]
    assert json.dumps(observer.SHELL_PROBE_COMMAND) in prompt
    assert str(worktree) not in prompt
    assert "b64decode" not in prompt


def test_agent_report_and_allowed_file_do_not_prove_shell_denial(tmp_path: Path):
    runtime, binary = _runtime(tmp_path)
    allowed, blocked = tmp_path / "allowed", tmp_path / "blocked"
    allowed.write_text("allowed")
    stream = tmp_path / "claimed-denial.jsonl"
    stream.write_text("\n".join(json.dumps(item) for item in [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text":
            f"TOKEN PermissionError: permission denied: {blocked}"}},
        {"type": "turn.completed"},
    ]))
    hooks = tmp_path / "hooks"
    hooks.write_text("\n".join(f"nonce {event}" for event in observer.HOOKS))
    record = observer.derive(runtime, binary, "nonce", stream, hooks, "TOKEN",
                             allowed=allowed, blocked=blocked)
    assert record["observed"]["shell_denied"] is False
    assert record["observed"]["write_boundary"] is False
