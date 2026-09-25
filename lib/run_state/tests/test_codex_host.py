"""Focused fail-closed tests for the qualified Codex host adapter."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from host_capabilities import (
    DISABLED_NATIVE_FEATURES,
    QualifiedCodexRuntime,
    codex_closed_environment,
    codex_environment_policy_hash,
)
from run_state.codex_host import CodexHostAdapter, CodexHostRefused, TelemetryRefused, parse_codex_telemetry
import run_state.codex_host as codex_host


USAGE = {
    "input_tokens": 7,
    "cached_input_tokens": 2,
    "cache_write_input_tokens": 1,
    "output_tokens": 3,
    "reasoning_output_tokens": 4,
}


def _stream(*records: object) -> bytes:
    return ("\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n").encode()


def _valid_stream() -> bytes:
    return _stream({"type": "thread.started", "thread_id": "thread-1"},
                   {"type": "turn.started"}, {"type": "turn.completed", "usage": USAGE})


def _runtime(tmp_path: Path) -> tuple[QualifiedCodexRuntime, Path, Path]:
    home, workspace, binary = tmp_path / "home", tmp_path / "workspace", tmp_path / "codex"
    home.mkdir(mode=0o700); workspace.mkdir(); binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    config = home / "config.toml"; config.write_text("approval_policy = 'never'\n")
    auth = home / "auth.json"; auth.write_text("{}\n"); auth.chmod(0o600)
    workspace_info = workspace.stat()
    policy_environment = codex_closed_environment(
        home, home / "ffs-codex-policy-tmp", binary, {"launcher_sha256": "a" * 64},
    )
    runtime = QualifiedCodexRuntime(
        binary=(("launcher_sha256", "a" * 64),),
        runtime=(("path", str(home)), ("config_sha256", hashlib.sha256(config.read_bytes()).hexdigest())),
        workspace=(("path", str(workspace)), ("device", workspace_info.st_dev), ("inode", workspace_info.st_ino)),
        supervisor=(),
        execution=(("model", "gpt-5.6-sol"), ("effort", "xhigh"), ("sandbox", "workspace-write"),
                   ("network_enabled", False), ("roots", [str(workspace)]),
                   ("disabled_features", list(DISABLED_NATIVE_FEATURES))),
        observation=(("environment_sha256", codex_environment_policy_hash(policy_environment)),),
    )
    return runtime, binary, workspace


def _gsd_environment(tmp_path: Path, name: str = "admission.json") -> dict[str, str]:
    admission = tmp_path / name
    admission.write_text('{"schema":"ffs.supervisor-admission/v1","available":true}\n')
    admission.chmod(0o600)
    bridge = tmp_path / "gsd_wave_bridge.py"
    bridge.write_text("#!/usr/bin/env python3\n")
    command = json.dumps([sys.executable, str(bridge)], ensure_ascii=True, separators=(",", ":"))
    return {
        "GSD_DISPATCH_MODE": "ffs-supervised-process",
        "FFS_SUPERVISED_COMMIT_MODE": "patches",
        "FFS_SUPERVISED_ADMISSION_FILE": str(admission),
        "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": command,
    }


def test_parser_accepts_one_complete_thread_and_exact_usage() -> None:
    parsed = parse_codex_telemetry(_valid_stream())
    assert parsed.thread_id == "thread-1"
    assert dict(parsed.token_usage) == USAGE
    assert parsed.byte_length == len(_valid_stream())


@pytest.mark.parametrize("stream", [
    b'{"type":"thread.started","type":"thread.started","thread_id":"x"}\n',
    b'\xff',
    b'{not-json}\n',
    _stream({"type": "thread.started", "thread_id": "x"}, {"type": "turn.completed", "usage": USAGE},
            {"type": "turn.completed", "usage": USAGE}),
    _stream({"type": "thread.started", "thread_id": "x"}, {"type": "turn.failed"},
            {"type": "turn.completed", "usage": USAGE}),
    _stream({"type": "thread.started", "thread_id": "x"}, {"type": "turn.stopped"},
            {"type": "turn.completed", "usage": USAGE}),
    _stream({"type": "thread.started", "thread_id": "x"},
            {"type": "turn.completed", "usage": {**USAGE, "extra": 1}}),
    _stream({"type": "thread.started", "thread_id": "x"},
            {"type": "turn.completed", "usage": {**USAGE, "total_tokens": 14}}),
    _stream({"type": "thread.started", "thread_id": "x"},
            {"type": "turn.completed", "usage": {**USAGE, "input_tokens": True}}),
    _stream({"type": "thread.started", "thread_id": "x"},
            {"type": "turn.completed", "usage": {**USAGE, "input_tokens": 2**63}}),
])
def test_parser_rejects_malformed_or_ambiguous_streams(stream: bytes) -> None:
    with pytest.raises(TelemetryRefused):
        parse_codex_telemetry(stream)


def test_parser_enforces_the_two_mebibyte_bound() -> None:
    with pytest.raises(TelemetryRefused, match="TOO_LARGE"):
        parse_codex_telemetry(b" " * (2 * 1024 * 1024 + 1))


def test_adapter_builds_the_exact_closed_invocation(monkeypatch, tmp_path: Path) -> None:
    runtime, binary, workspace = _runtime(tmp_path)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    observed = {}

    def run(argv, **kwargs):
        observed.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=_valid_stream(), stderr=b"")

    monkeypatch.setattr(codex_host.subprocess, "run", run)
    receipt = CodexHostAdapter(runtime, binary, "0.150.0").invoke("do work", attempt=3)
    assert receipt.status == "completed" and receipt.exit_code == 0 and receipt.stream is not None
    assert receipt.argv == (
        str(binary.resolve()), "exec", "--json", "-c", 'model="gpt-5.6-sol"', "-c",
        'model_reasoning_effort="xhigh"', "--strict-config", "--ignore-user-config", "--ignore-rules",
        "--dangerously-bypass-hook-trust", "--sandbox", "workspace-write",
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true", "--disable", "multi_agent",
        "--disable", "multi_agent_v2", "--disable", "plugins", "--disable", "remote_plugin",
        "--disable", "recommended_plugins", "--disable", "plugin_sharing", "--disable", "apps",
        "--cd", str(workspace), "--color", "never", "do work",
    )
    assert observed["cwd"] == str(workspace) and observed["stdin"] is subprocess.DEVNULL
    assert observed["env"]["HOME"] == str(tmp_path / "home")
    assert observed["env"]["CODEX_HOME"] == str(tmp_path / "home")
    assert Path(observed["env"]["TMPDIR"]).parent == tmp_path / "home"
    assert str(binary.parent) in observed["env"]["PATH"].split(":")
    assert set(observed["env"]) == {"HOME", "CODEX_HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "NO_COLOR"}
    assert not any("API_KEY" in key for key in observed["env"])
    assert not Path(observed["env"]["TMPDIR"]).exists()
    assert receipt.environment_sha256 == hashlib.sha256(
        json.dumps(observed["env"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_policy_hash_is_stable_across_random_invocation_temp_leaves(monkeypatch, tmp_path: Path) -> None:
    runtime, binary, _workspace = _runtime(tmp_path)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    adapter = CodexHostAdapter(runtime, binary, "0.150.0")
    first = adapter.build_launch_material("one", attempt=0)
    second = adapter.build_launch_material("two", attempt=1)
    try:
        assert dict(first.environment)["TMPDIR"] != dict(second.environment)["TMPDIR"]
        assert runtime.observation[0][1] == codex_host.codex_environment_policy_hash(
            dict(first.environment)
        )
        assert runtime.observation[0][1] == codex_host.codex_environment_policy_hash(
            dict(second.environment)
        )
    finally:
        adapter.release_launch_material(first)
        adapter.release_launch_material(second)


def test_adapter_binds_typed_gsd_environment_and_normalizes_admission_leaf(monkeypatch, tmp_path: Path) -> None:
    runtime, binary, _workspace = _runtime(tmp_path)
    first_additions = _gsd_environment(tmp_path, "admission-one.json")
    second_additions = _gsd_environment(tmp_path, "admission-two.json")
    policy_environment = codex_host.codex_closed_environment(
        tmp_path / "home", tmp_path / "home" / "ffs-codex-policy-tmp", binary,
        {"launcher_sha256": "a" * 64}, first_additions,
    )
    runtime = replace(runtime, observation=(("environment_sha256", codex_environment_policy_hash(policy_environment)),))
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    adapter = CodexHostAdapter(runtime, binary, "0.150.0")
    first = adapter.build_launch_material("one", attempt=0, gsd_environment=first_additions)
    second = adapter.build_launch_material("two", attempt=1, gsd_environment=second_additions)
    try:
        first_env, second_env = dict(first.environment), dict(second.environment)
        assert first_env["GSD_DISPATCH_MODE"] == "ffs-supervised-process"
        assert first_env["FFS_SUPERVISED_COMMIT_MODE"] == "patches"
        assert first_env["FFS_SUPERVISED_DISPATCH_COMMAND_JSON"] == second_env["FFS_SUPERVISED_DISPATCH_COMMAND_JSON"]
        assert first_env["FFS_SUPERVISED_ADMISSION_FILE"] != second_env["FFS_SUPERVISED_ADMISSION_FILE"]
        assert codex_environment_policy_hash(first_env) == codex_environment_policy_hash(second_env)
        assert hashlib.sha256(json.dumps(first_env, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != hashlib.sha256(
            json.dumps(second_env, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    finally:
        adapter.release_launch_material(first)
        adapter.release_launch_material(second)


def test_launch_env_carries_scope_and_policy_binds_it(monkeypatch, tmp_path: Path) -> None:
    """F34 5.6: launch env has GSD_PROJECT. The same qualified runtime with a
    different project gives ENVIRONMENT_POLICY_DRIFT. Rules out an unbound,
    launch-only value."""
    runtime, binary, _workspace = _runtime(tmp_path)
    additions = _gsd_environment(tmp_path)
    scoped_additions = {**additions, "GSD_PROJECT": "demo-project"}
    policy_environment = codex_host.codex_closed_environment(
        tmp_path / "home", tmp_path / "home" / "ffs-codex-policy-tmp", binary,
        {"launcher_sha256": "a" * 64}, scoped_additions,
    )
    runtime = replace(runtime, observation=(("environment_sha256", codex_environment_policy_hash(policy_environment)),))
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    adapter = CodexHostAdapter(runtime, binary, "0.150.0")
    material = adapter.build_launch_material("do work", attempt=0, gsd_environment=scoped_additions)
    try:
        assert dict(material.environment)["GSD_PROJECT"] == "demo-project"
    finally:
        adapter.release_launch_material(material)

    other_project_additions = {**additions, "GSD_PROJECT": "other-project"}
    with pytest.raises(CodexHostRefused, match="ENVIRONMENT_POLICY_DRIFT"):
        CodexHostAdapter(runtime, binary, "0.150.0").build_launch_material(
            "do work", attempt=1, gsd_environment=other_project_additions,
        )


def test_adapter_rejects_untyped_or_noncanonical_gsd_environment(monkeypatch, tmp_path: Path) -> None:
    runtime, binary, _workspace = _runtime(tmp_path)
    additions = _gsd_environment(tmp_path)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    for mutation in (
        {**additions, "unexpected": "value"},
        {**additions, "GSD_DISPATCH_MODE": "inline"},
        {**additions, "FFS_SUPERVISED_DISPATCH_COMMAND_JSON": json.dumps(
            json.loads(additions["FFS_SUPERVISED_DISPATCH_COMMAND_JSON"])
        )},
    ):
        with pytest.raises(CodexHostRefused, match="GSD_ENVIRONMENT_INVALID"):
            CodexHostAdapter(runtime, binary, "0.150.0").build_launch_material(
                "do work", attempt=0, gsd_environment=mutation,
            )


def test_adapter_refuses_observed_environment_policy_mismatch(monkeypatch, tmp_path: Path) -> None:
    runtime, binary, _workspace = _runtime(tmp_path)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    drifted = replace(runtime, observation=(("environment_sha256", "0" * 64),))
    with pytest.raises(CodexHostRefused, match="ENVIRONMENT_POLICY_DRIFT"):
        CodexHostAdapter(drifted, binary, "0.150.0").build_launch_material("do work", attempt=0)


def test_build_launch_material_is_immutable_and_does_not_start_a_process(monkeypatch, tmp_path: Path) -> None:
    runtime, binary, workspace = _runtime(tmp_path)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    adapter = CodexHostAdapter(runtime, binary, "0.150.0")
    material = adapter.build_launch_material("do work", attempt=8)
    try:
        assert material.argv[-1] == "do work" and material.cwd == str(workspace)
        assert material.execution_environment()["CODEX_HOME"] == str(tmp_path / "home")
        assert material.attempt == 8 and Path(material.temporary_dir).is_dir()
    finally:
        adapter.release_launch_material(material)
    assert not Path(material.temporary_dir).exists()


@pytest.mark.parametrize("stdout", [b"", b"bad\n"])
def test_adapter_returns_uncertain_receipt_for_missing_or_bad_telemetry(monkeypatch, tmp_path: Path, stdout: bytes) -> None:
    runtime, binary, _workspace = _runtime(tmp_path)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    monkeypatch.setattr(codex_host.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout, b""))
    receipt = CodexHostAdapter(runtime, binary, "0.150.0").invoke("do work", attempt=0)
    assert receipt.status == "uncertain" and receipt.stream is None and receipt.exit_code == 1


def test_adapter_refuses_drifted_config_and_unqualified_argv(monkeypatch, tmp_path: Path) -> None:
    runtime, binary, _workspace = _runtime(tmp_path)
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: {"launcher_sha256": "a" * 64})
    (tmp_path / "home" / "config.toml").write_text("changed")
    adapter = CodexHostAdapter(runtime, binary, "0.150.0")
    with pytest.raises(CodexHostRefused, match="CONFIG_DRIFT"):
        adapter.invoke("do work", attempt=1)
    with pytest.raises(TypeError):
        adapter.invoke("do work", attempt=1, argv=("untrusted",))  # type: ignore[call-arg]


@pytest.mark.parametrize("change", ["binary", "network", "root", "native_feature", "model"])
def test_adapter_refuses_drifted_or_unsafe_qualified_material(monkeypatch, tmp_path: Path, change: str) -> None:
    runtime, binary, workspace = _runtime(tmp_path)
    chain = {"launcher_sha256": "a" * 64}
    if change == "binary":
        chain = {"launcher_sha256": "b" * 64}
    else:
        execution = dict(runtime.execution)
        if change == "network":
            execution["network_enabled"] = True
        elif change == "root":
            execution["roots"] = [str(workspace), str(tmp_path)]
        elif change == "native_feature":
            execution["disabled_features"] = ["multi_agent"]
        else:
            execution["model"] = 'safe"\nweb_search="live'
        runtime = replace(runtime, execution=tuple(execution.items()))
    monkeypatch.setattr(codex_host, "_binary_chain", lambda path: chain)
    with pytest.raises(CodexHostRefused):
        CodexHostAdapter(runtime, binary, "0.150.0").build_launch_material("do work", attempt=2)
