"""Native review evidence parser; fixtures do not claim live model qualification."""
import json
import hashlib
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from run_state.codex_host import CodexHostRefused, verify_artifact_review_session
from run_state import native_review_runtime as review_runtime
from test_native_review_runtime import _inputs, _request


def test_actual_session_model_and_zero_tools_are_required(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    sessions = runtime / "sessions"
    sessions.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = sessions / "rollout-date-thread-1.jsonl"
    context = {"cwd": str(workspace), "model": "gpt-5.6-sol", "effort": "high", "turn_id": "turn-1"}
    records = [{"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(workspace)}},
               {"type": "turn_context", "payload": context},
               {"type": "response_item", "payload": {"type": "message", "role": "assistant"}}]

    def write():
        path.write_text("\n".join(json.dumps(row) for row in records) + "\n")

    def verify():
        return verify_artifact_review_session(runtime, thread_id="thread-1", workspace=workspace,
                                              model="gpt-5.6-sol", effort="high")

    write()
    proof = verify()
    assert proof["effective_model"] == "gpt-5.6-sol" and proof["turn_id"] == "turn-1"
    context["model"] = "different-model"
    write()
    with pytest.raises(CodexHostRefused, match="ARTIFACT_REVIEW_MODEL_UNPROVEN"):
        verify()
    context["model"] = "gpt-5.6-sol"
    records.append({"type": "response_item", "payload": {"type": "function_call", "name": "shell"}})
    write()
    with pytest.raises(CodexHostRefused, match="ARTIFACT_REVIEW_TOOL_USE"):
        verify()
    records.pop()
    records.append({"type": "turn_context", "payload": dict(context)})
    write()
    with pytest.raises(CodexHostRefused, match="ARTIFACT_REVIEW_MODEL_UNPROVEN"):
        verify()
    records.pop()
    write()
    (sessions / "alias").symlink_to(path)
    with pytest.raises(CodexHostRefused, match="ARTIFACT_REVIEW_MODEL_UNPROVEN"):
        verify()


def _review_evidence(tmp_path):
    """Scripted files only: no native binary or model is executed."""
    parent, workspace, binary, catalog = _inputs(tmp_path)
    material = review_runtime.prepare_native_review_runtime(
        _request(binary, catalog), runtime_root=parent / "review", workspace=workspace,
    )
    sessions = Path(material.runtime_root) / "sessions"
    sessions.mkdir()
    path = sessions / "rollout-fixture-thread-1.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(workspace)}},
        {"type": "turn_context", "payload": {
            "cwd": str(workspace), "model": material.requested_model,
            "effort": material.effort, "turn_id": "turn-1",
        }},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
    ]
    path.write_bytes(_jsonl(records))
    usage = {"input_tokens": 7, "cached_input_tokens": 2, "output_tokens": 3,
             "cache_write_input_tokens": 1, "reasoning_output_tokens": 4}
    stream = [{"type": "thread.started", "thread_id": "thread-1"},
              {"type": "turn.started"}, {"type": "turn.completed", "usage": usage}]
    return material, path, records, stream


def _jsonl(records):
    return ("\n".join(json.dumps(row) for row in records) + "\n").encode()


def test_codex_review_observation_binds_material_telemetry_and_retained_session(tmp_path):
    material, path, _records, stream = _review_evidence(tmp_path)
    raw = _jsonl(stream)
    observation = review_runtime.verify_codex_review_evidence(material, raw, exit_code=0)

    assert observation.material_sha256 == hashlib.sha256(json.dumps(
        asdict(material), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert observation.session_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert observation.telemetry.sha256 == hashlib.sha256(raw).hexdigest()
    assert observation.telemetry.thread_id == "thread-1"
    assert observation.turn_id == "turn-1"
    assert observation.effective_model == material.requested_model
    assert observation.effective_effort == material.effort
    assert dict(observation.telemetry.token_usage) == stream[-1]["usage"]
    assert review_runtime.verify_codex_review_evidence(material, raw, exit_code=0) == observation
    # An observation is deliberately not a QualifiedCodexRuntime or an authority receipt.
    assert not hasattr(observation, "status")


@pytest.mark.parametrize("mutation", [
    "thread", "missing_terminal", "usage", "model", "effort", "workspace", "tool", "runtime",
])
def test_codex_review_observation_refuses_mismatched_or_incomplete_evidence(tmp_path, mutation):
    material, path, records, stream = _review_evidence(tmp_path)
    if mutation == "thread":
        stream[0]["thread_id"] = "different-thread"
    elif mutation == "missing_terminal":
        stream.pop()
    elif mutation == "usage":
        stream[-1]["usage"]["input_tokens"] = True
    elif mutation in {"model", "effort", "workspace"}:
        records[1]["payload"]["cwd" if mutation == "workspace" else mutation] = "wrong"
    elif mutation == "tool":
        records.append({"type": "response_item", "payload": {"type": "function_call", "name": "shell"}})
    else:
        # An otherwise valid rollout in a different runtime cannot satisfy this material.
        alternate = tmp_path / "alternate-runtime"
        alternate.mkdir(mode=0o700)
        path.parent.rename(alternate / "sessions")
        path = alternate / "sessions" / path.name
    path.write_bytes(_jsonl(records))
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused):
        review_runtime.verify_codex_review_evidence(material, _jsonl(stream), exit_code=0)


@pytest.mark.parametrize("exit_code", [1, None, False])
def test_codex_review_observation_requires_successful_exact_exit_code(tmp_path, exit_code):
    material, _path, _records, stream = _review_evidence(tmp_path)
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused):
        review_runtime.verify_codex_review_evidence(material, _jsonl(stream), exit_code=exit_code)


def test_codex_review_observation_revalidates_closure_before_session_read(tmp_path, monkeypatch):
    material, _path, _records, stream = _review_evidence(tmp_path)
    Path(material.config_path).write_text("web_search = 'live'\n")

    def must_not_read(*args, **kwargs):
        pytest.fail("material drift must refuse before session evidence is read")

    monkeypatch.setattr("run_state.codex_host.verify_artifact_review_session", must_not_read)
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused, match="closure drifted"):
        review_runtime.verify_codex_review_evidence(material, _jsonl(stream), exit_code=0)


def test_codex_review_observation_binds_workspace_inode_even_at_same_path(tmp_path):
    material, _path, _records, stream = _review_evidence(tmp_path)
    raw = _jsonl(stream)
    original = review_runtime.verify_codex_review_evidence(material, raw, exit_code=0)
    workspace = Path(material.workspace)
    workspace.rename(tmp_path / "old-workspace")
    workspace.mkdir(mode=0o755)
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused, match="workspace was replaced"):
        review_runtime.verify_codex_review_evidence(material, raw, exit_code=0)
    info = workspace.stat()
    rebound = replace(material, workspace_device=info.st_dev, workspace_inode=info.st_ino)
    assert rebound.replay_binding() == material.replay_binding()
    observation = review_runtime.verify_codex_review_evidence(rebound, raw, exit_code=0)
    assert observation.material_sha256 != original.material_sha256


def test_codex_review_observation_refuses_other_host_material(tmp_path):
    parent, workspace, binary, catalog = _inputs(tmp_path)
    request = replace(_request(binary, catalog), host="claude",
                      cli_version=review_runtime.CLAUDE_CLI_VERSION,
                      session_id="643b3a28-33d2-4000-b983-a18c5da41bbd")
    material = review_runtime.prepare_native_review_runtime(
        request, runtime_root=parent / "claude", workspace=workspace,
    )
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused, match="Codex"):
        review_runtime.verify_codex_review_evidence(material, b"", exit_code=0)


def test_codex_review_observation_refuses_replaced_runtime(tmp_path):
    material, _path, _records, stream = _review_evidence(tmp_path)
    root = Path(material.runtime_root)
    root.rename(root.with_name("retained-old-runtime"))
    root.mkdir(mode=0o700)
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused, match="runtime root was replaced"):
        review_runtime.verify_codex_review_evidence(material, _jsonl(stream), exit_code=0)


def test_codex_review_observation_refuses_closure_drift_during_session_read(tmp_path, monkeypatch):
    material, _path, _records, stream = _review_evidence(tmp_path)

    def read_then_drift(*args, **kwargs):
        proof = verify_artifact_review_session(*args, **kwargs)
        Path(material.config_path).write_text("web_search = 'live'\n")
        return proof

    monkeypatch.setattr("run_state.codex_host.verify_artifact_review_session", read_then_drift)
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused, match="closure drifted"):
        review_runtime.verify_codex_review_evidence(material, _jsonl(stream), exit_code=0)


def _claude_review_evidence(tmp_path):
    """Pinned-profile stream shape, entirely scripted; no auth/model proof."""
    parent, workspace, binary, catalog = _inputs(tmp_path)
    request = replace(_request(binary, catalog), host="claude", requested_model="claude-opus-4-6",
                      cli_version=review_runtime.CLAUDE_CLI_VERSION,
                      session_id="643b3a28-33d2-4000-b983-a18c5da41bbd")
    material = review_runtime.prepare_native_review_runtime(
        request, runtime_root=parent / "claude", workspace=workspace,
    )
    records = [
        {"type": "system", "subtype": "init", "session_id": request.session_id,
         "cwd": str(workspace), "model": request.requested_model,
         "claude_code_version": request.cli_version, "tools": [], "mcp_servers": [],
         "slash_commands": [], "skills": [], "plugins": []},
        {"type": "assistant", "session_id": request.session_id, "parent_tool_use_id": None,
         "message": {"role": "assistant", "model": request.requested_model,
                     "content": [{"type": "text", "text": "Fixture review output."}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": request.session_id, "num_turns": 1, "stop_reason": "end_turn",
         "permission_denials": [], "usage": {
             "input_tokens": 3, "cache_creation_input_tokens": 1,
             "cache_read_input_tokens": 2, "output_tokens": 4,
             "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
             "output_tokens_details": {"thinking_tokens": 0},
         }, "modelUsage": {request.requested_model: {
             "canonicalModel": request.requested_model, "webSearchRequests": 0,
         }}},
    ]
    return material, records


def test_claude_review_observation_binds_uuid_material_and_typed_usage_without_claiming_effort(tmp_path):
    material, records = _claude_review_evidence(tmp_path)
    raw = _jsonl(records)
    observation = review_runtime.verify_claude_review_evidence(material, raw, exit_code=0)
    assert observation.material_sha256 == hashlib.sha256(json.dumps(
        asdict(material), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert observation.telemetry.sha256 == hashlib.sha256(raw).hexdigest()
    assert observation.telemetry.session_id == material.session_id
    assert observation.telemetry.effective_model == material.requested_model
    assert observation.telemetry.cli_version == material.cli_version
    assert dict(observation.telemetry.token_usage) == {
        "input_tokens": 3, "cache_creation_input_tokens": 1,
        "cache_read_input_tokens": 2, "output_tokens": 4,
    }
    assert not hasattr(observation, "effective_effort")
    assert not hasattr(observation.telemetry, "effective_effort")
    assert review_runtime.verify_claude_review_evidence(material, raw, exit_code=0) == observation


@pytest.mark.parametrize("mutation", [
    "init_session", "result_session", "assistant_session", "model", "assistant_model",
    "version", "workspace", "tools", "missing_tools", "mcp", "tool_use", "tool_result",
    "top_level_tool", "hook", "missing_usage", "invalid_usage", "missing_terminal",
    "failed_result", "quota_banner", "permission_denial", "no_assistant", "model_usage",
    "server_tool", "max_turns", "init_order",
])
def test_claude_review_observation_refuses_unproven_or_tool_enabled_stream(tmp_path, mutation):
    material, records = _claude_review_evidence(tmp_path)
    if mutation in {"init_session", "result_session", "assistant_session"}:
        records[{"init_session": 0, "result_session": -1, "assistant_session": 1}[mutation]]["session_id"] = "wrong"
    elif mutation in {"model", "version", "workspace"}:
        records[0][{"model": "model", "version": "claude_code_version", "workspace": "cwd"}[mutation]] = "wrong"
    elif mutation == "assistant_model":
        records[1]["message"]["model"] = "wrong"
    elif mutation == "tools":
        records[0]["tools"] = ["Bash"]
    elif mutation == "missing_tools":
        del records[0]["tools"]
    elif mutation == "mcp":
        records[0]["mcp_servers"] = [{"name": "unsafe"}]
    elif mutation in {"tool_use", "tool_result"}:
        records[1]["message"]["content"].append({"type": mutation})
    elif mutation in {"top_level_tool", "hook"}:
        records.insert(2, {"type": "tool"} if mutation == "top_level_tool" else
                       {"type": "system", "subtype": "hook_started", "hook_name": "PreToolUse"})
    elif mutation == "missing_usage":
        del records[-1]["usage"]
    elif mutation == "invalid_usage":
        records[-1]["usage"]["output_tokens"] = True
    elif mutation == "missing_terminal":
        records.pop()
    elif mutation == "failed_result":
        records[-1]["is_error"] = True
    elif mutation == "permission_denial":
        records[-1]["permission_denials"] = [{"tool_name": "Bash"}]
    elif mutation == "no_assistant":
        records.pop(1)
    elif mutation == "model_usage":
        records[-1]["modelUsage"]["other-model"] = {}
    elif mutation == "server_tool":
        records[-1]["usage"]["server_tool_use"]["web_search_requests"] = 1
    elif mutation == "max_turns":
        records[-1]["subtype"] = "success_max_turns"
    elif mutation == "init_order":
        records[0], records[1] = records[1], records[0]
    raw = b"You've hit your weekly limit" if mutation == "quota_banner" else _jsonl(records)
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused):
        review_runtime.verify_claude_review_evidence(material, raw, exit_code=0)


@pytest.mark.parametrize("exit_code", [1, None, False])
def test_claude_review_observation_refuses_unsuccessful_exit_even_with_valid_stream(tmp_path, exit_code):
    material, records = _claude_review_evidence(tmp_path)
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused):
        review_runtime.verify_claude_review_evidence(material, _jsonl(records), exit_code=exit_code)


def test_claude_review_observation_revalidates_closure_before_and_after_parsing(tmp_path, monkeypatch):
    from run_state.claude_host import parse_claude_telemetry

    material, records = _claude_review_evidence(tmp_path)
    calls = []

    def parse_then_drift(*args, **kwargs):
        calls.append(True)
        telemetry = parse_claude_telemetry(*args, **kwargs)
        Path(material.mcp_path).write_text('{"mcpServers":{"unsafe":{}}}')
        return telemetry

    monkeypatch.setattr("run_state.claude_host.parse_claude_telemetry", parse_then_drift)
    for _ in range(2):
        with pytest.raises(review_runtime.NativeReviewRuntimeRefused, match="MCP closure drifted"):
            review_runtime.verify_claude_review_evidence(material, _jsonl(records), exit_code=0)
    assert calls == [True]


def test_claude_review_observation_refuses_codex_material(tmp_path):
    material, _path, _records, stream = _review_evidence(tmp_path)
    with pytest.raises(review_runtime.NativeReviewRuntimeRefused, match="Claude"):
        review_runtime.verify_claude_review_evidence(material, _jsonl(stream), exit_code=0)
