from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest

from host_capabilities import _binary_chain
from run_state.claude_host import (
    ClaudeHostAdapter, ClaudeHostRefused, ClaudeTelemetryRefused,
    QualifiedClaudeRuntime, claude_closed_environment, claude_environment_policy_hash,
    parse_claude_host_request, parse_claude_telemetry,
)
from run_state.claude_runtime_staging import STAGE_MANIFEST_NAME, stage_private_claude_runtime


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    owned = candidate / "gsd-core" / "bin" / "gsd-tools.cjs"
    owned.parent.mkdir(parents=True)
    owned.write_text("gsd")
    bridge = candidate / "lib" / "feature-fix-swarm" / "run_state" / "gsd_wave_bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("# bridge\n")
    hook = candidate / "hooks" / "fixture.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    settings = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": str(hook)}]}]}}
    (candidate / "settings.json").write_text(json.dumps(settings))
    manifest = {"version": "1.14.0", "runtime": "claude", "files": {
        "gsd-core/bin/gsd-tools.cjs": _sha(owned)}}
    (candidate / "gsd-file-manifest.json").write_text(json.dumps(manifest))
    credential = tmp_path / "credential.json"
    credential.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "fixture-access", "refreshToken": "excluded-refresh",
                           "expiresAt": 9999999999999, "refreshTokenExpiresAt": 9999999999999,
                           "scopes": ["user:inference"], "subscriptionType": "fixture",
                           "rateLimitTier": "fixture"},
        "mcpOAuth": {"excluded": True},
    }))
    credential.chmod(0o600)
    return candidate, credential


def test_private_stage_is_manifest_bound_and_does_not_mutate_sources(tmp_path: Path) -> None:
    candidate, credential = _candidate(tmp_path)
    workspace = tmp_path / "work"
    workspace.mkdir()
    parent = tmp_path / "stages"
    parent.mkdir(mode=0o700)
    before = (credential.read_bytes(), credential.stat().st_ino)
    result = stage_private_claude_runtime(candidate, credential, parent / "runtime", workspace)
    runtime = parent / "runtime"
    assert result["schema"] == "ffs.private-claude-runtime-stage/v1"
    assert (credential.read_bytes(), credential.stat().st_ino) == before
    assert (runtime / ".credentials.json").stat().st_mode & 0o777 == 0o600
    assert (runtime / ".credentials.json").stat().st_ino != credential.stat().st_ino
    assert set(json.loads((runtime / ".credentials.json").read_text())) == {"claudeAiOauth"}
    projected = json.loads((runtime / ".credentials.json").read_text())["claudeAiOauth"]
    assert projected["accessToken"] == "fixture-access"
    assert projected["refreshToken"] is None and projected["refreshTokenExpiresAt"] is None
    assert "excluded-refresh" not in (runtime / ".credentials.json").read_text()
    settings = json.loads((runtime / "settings.json").read_text())
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["network"]["deniedDomains"] == ["*"]
    assert settings["sandbox"]["filesystem"]["denyRead"] == [str(runtime / ".credentials.json")]
    assert settings["permissions"]["deny"] == ["Agent", "Task", "WebFetch", "WebSearch"]
    assert str(candidate) not in (runtime / "settings.json").read_text()
    assert (runtime / STAGE_MANIFEST_NAME).stat().st_mode & 0o777 == 0o600


def test_private_stage_refuses_drift_and_unsafe_credentials(tmp_path: Path) -> None:
    candidate, credential = _candidate(tmp_path)
    workspace = tmp_path / "work"
    workspace.mkdir()
    parent = tmp_path / "stages"
    parent.mkdir()
    (candidate / "gsd-core/bin/gsd-tools.cjs").write_text("drift")
    with pytest.raises(ValueError, match="drifted"):
        stage_private_claude_runtime(candidate, credential, parent / "one", workspace)
    candidate, credential = _candidate(tmp_path / "second")
    credential.chmod(0o644)
    with pytest.raises(ValueError, match="private 0600"):
        stage_private_claude_runtime(candidate, credential, parent / "two", workspace)


def _stream(model: str, session: str, *, hook: bool = False, nested: bool = False) -> bytes:
    values = [{"type": "system", "subtype": "init", "session_id": session, "model": model,
               "claude_code_version": "2.1.274"}]
    if hook:
        values.append({"type": "system", "subtype": "hook_started", "hook_name": "PreToolUse"})
    if nested:
        values.append({"type": "assistant", "message": {"content": [{"text": '{"loggedIn":false}'}]}})
    values.append({"type": "result", "subtype": "success", "is_error": False,
                   "session_id": session, "usage": {"input_tokens": 3,
                   "cache_creation_input_tokens": 1, "cache_read_input_tokens": 2,
                   "output_tokens": 4}})
    return ("\n".join(json.dumps(value) for value in values) + "\n").encode()


def test_telemetry_binds_effective_model_session_usage_and_hooks() -> None:
    session = str(uuid.uuid4())
    stream = _stream("claude-opus-5", session, hook=True)
    result = parse_claude_telemetry(stream, requested_model="claude-opus-5", expected_session_id=session)
    assert result.effective_model == "claude-opus-5"
    assert dict(result.token_usage)["output_tokens"] == 4
    assert result.hook_events == ("PreToolUse",)
    with pytest.raises(ClaudeTelemetryRefused, match="EFFECTIVE_MODEL_MISMATCH"):
        parse_claude_telemetry(stream, requested_model="claude-fable-5", expected_session_id=session)
    with pytest.raises(ClaudeTelemetryRefused, match="SESSION_ID_MISMATCH"):
        parse_claude_telemetry(stream, requested_model="claude-opus-5", expected_session_id=str(uuid.uuid4()))


def test_request_preserves_exact_model_and_rejects_network(tmp_path: Path) -> None:
    values = dict(runtime_home=str(tmp_path.resolve()), credential_source=str((tmp_path / "auth").resolve()),
                  binary=str((tmp_path / "claude").resolve()), sandbox="workspace-write",
                  network_enabled=False, token_reservation=9, timeout_seconds=60)
    request = parse_claude_host_request(model_request_json='{"kind":"exact","id":"claude-opus-5"}', **values)
    assert request.model == "claude-opus-5" and request.effort is None
    with pytest.raises(ClaudeHostRefused, match="HOST_REQUEST_INVALID"):
        parse_claude_host_request(model_request_json='{"kind":"tier","name":"execution"}',
                                  **{**values, "network_enabled": True})


def test_launch_material_has_closed_cli_and_private_auth_guard(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    candidate, credential_source = _candidate(tmp_path)
    workspace, stage_parent = tmp_path / "work", tmp_path / "stages"
    workspace.mkdir()
    stage_parent.mkdir(mode=0o700)
    runtime = stage_parent / "runtime"
    stage_private_claude_runtime(candidate, credential_source, runtime, workspace)
    settings = runtime / "settings.json"
    credential = runtime / ".credentials.json"
    credential_bytes = credential.read_bytes()
    info, runtime_info = workspace.stat(), runtime.stat()
    environment = claude_closed_environment(runtime, runtime / "placeholder", binary)
    qualified = QualifiedClaudeRuntime(
        tuple(sorted(_binary_chain(binary).items())),
        tuple(sorted({"path": str(runtime), "device": runtime_info.st_dev, "inode": runtime_info.st_ino,
                      "settings_sha256": _sha(settings),
                      "stage_sha256": _sha(runtime / STAGE_MANIFEST_NAME)}.items())),
        tuple(sorted({"path": str(workspace), "device": info.st_dev, "inode": info.st_ino}.items())),
        (("fixture", True),),
        tuple(sorted({"model": "claude-sonnet-5", "effort": None, "sandbox": "workspace-write",
                      "network_enabled": False, "roots": [str(workspace)], "tools": "fixture"}.items())),
        (("environment_sha256", claude_environment_policy_hash(environment)),),
    )
    material = ClaudeHostAdapter(qualified, binary, "2.1.274").build_launch_material(
        "do the work", attempt=1, session_id=str(uuid.uuid4()))
    try:
        assert material.argv[1:3] == ("-p", "do the work")
        assert material.argv[material.argv.index("--model") + 1] == "claude-sonnet-5"
        assert "--no-session-persistence" in material.argv
        assert "--strict-mcp-config" in material.argv
        assert material.argv[material.argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        assert "--fallback-model" not in material.argv
        assert set(material.execution_environment()) == {
            "HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "PATH", "LANG", "LC_ALL", "NO_COLOR", "CI",
            "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE",
        }
        auth_info = credential.stat()
        assert (material.credential_device, material.credential_inode) == (auth_info.st_dev, auth_info.st_ino)
        assert material.credential_sha256 == _sha(credential)
    finally:
        ClaudeHostAdapter.release_launch_material(material)
    credential.write_bytes(credential_bytes)
    credential.chmod(0o600)
    os.link(credential, runtime / "linked-auth")
    with pytest.raises(ClaudeHostRefused, match="RUNTIME_CREDENTIAL_UNSAFE"):
        ClaudeHostAdapter(qualified, binary, "2.1.274").build_launch_material(
            "do the work", attempt=2, session_id=str(uuid.uuid4()))
