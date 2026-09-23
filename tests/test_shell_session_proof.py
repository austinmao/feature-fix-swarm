"""Raw-session acceptance; these constructed records are hermetic, not live proof."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("shell_session_observer", ROOT / "scripts/gsd/codex-runtime-observer.py")
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)


@pytest.mark.parametrize("defect", [None, "native-default-namespace", "agent-only", "wrong-call", "wrong-turn", "wrong-thread",
    "wrong-model", "wrong-effort", "wrong-workspace", "replayed-nonce", "spoofed-program",
    "wrong-command", "success-exit", "no-denial", "duplicate-output", "wrong-tool", "wrong-namespace",
    "extra-pre-tool", "extra-post-tool"])
def test_shell_denial_requires_exact_machine_program_and_matching_session(tmp_path: Path, defect: str | None):
    runtime = tmp_path / "runtime"
    sessions = runtime / "sessions" / "2026" / "09" / "12"
    sessions.mkdir(parents=True, mode=0o700)
    (runtime / "config.toml").write_text("x")
    (runtime / "hooks.json").write_text("{}")
    (runtime / "gsd-file-manifest.json").write_text("{}")
    for name in ("skills", "agents", "gsd-core", "scripts"):
        (runtime / name).mkdir(mode=0o700)
        (runtime / name / "fixture").write_text("fixture")
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    worktree = tmp_path / "worktree"
    worktree.mkdir(mode=0o700)
    allowed, blocked = worktree / "allowed", tmp_path / "outside"
    allowed.write_text("allowed")
    nonce = "acceptance-shell-nonce"
    thread = str(uuid.uuid5(uuid.NAMESPACE_URL, "ffs-shell-thread"))
    turn = str(uuid.uuid5(uuid.NAMESPACE_URL, "ffs-shell-turn"))
    call = "call_shell_fixture"
    transcript = runtime / "ordinary.jsonl"
    write_rows(transcript, [{"type": "thread.started", "thread_id": thread},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "TOKEN"}},
        {"type": "turn.completed"}])
    hooks = runtime / "observed-hooks"
    hooks.write_text("\n".join(f"{nonce} {event}" for event in observer.HOOKS))

    def path_expression(path: Path) -> str:
        encoded = base64.b64encode(os.fsencode(path)).decode("ascii")
        return f'__import__("base64").b64decode("{encoded}").decode()'

    python = ("from pathlib import Path; "
              f"Path({path_expression(allowed)}).write_text(\"allowed\"); "
              f"Path({path_expression(blocked)}).write_text(\"blocked\")")
    command = "python3 -c " + shlex.quote(python)
    program = ("const command=" + json.dumps(command)
               + ";const result=await tools.exec_command({cmd:command,yield_time_ms:10000,max_output_tokens:12000});text(JSON.stringify({nonce:"
               + json.dumps(nonce) + ",command"
               + ",exit_code:result.exit_code,output:result.output}))")
    machine = {"nonce": nonce, "command": command, "exit_code": 1,
               "output": f"PermissionError: [Errno 1] Operation not permitted: '{blocked}'"}
    rows = [
        {"type": "session_meta", "payload": {"id": thread, "session_id": thread,
            "cwd": str(worktree), "model_provider": "openai"}},
        {"type": "turn_context", "payload": {"turn_id": turn, "cwd": str(worktree),
            "model": "gpt-5.6-terra", "effort": "medium"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
            "namespace": "functions", "call_id": call, "input": program,
            "internal_chat_message_metadata_passthrough": {"turn_id": turn}}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call,
            "output": [], "internal_chat_message_metadata_passthrough": {"turn_id": turn}}},
    ]
    if defect == "wrong-call": rows[3]["payload"]["call_id"] = "another-call"
    elif defect == "wrong-turn": rows[3]["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = "another-turn"
    elif defect == "wrong-thread": rows[0]["payload"]["id"] = "another-thread"
    elif defect == "wrong-model": rows[1]["payload"]["model"] = "gpt-5.6-sol"
    elif defect == "wrong-effort": rows[1]["payload"]["effort"] = "high"
    elif defect == "wrong-workspace": rows[1]["payload"]["cwd"] = str(tmp_path / "sibling")
    elif defect == "replayed-nonce": machine["nonce"] = "earlier-nonce"
    elif defect == "spoofed-program": rows[2]["payload"]["input"] = "text('PermissionError')"
    elif defect == "wrong-command": machine["command"] = "echo PermissionError"
    elif defect == "success-exit": machine["exit_code"] = 0
    elif defect == "no-denial": machine["output"] = "unrelated failure"
    elif defect == "wrong-tool": rows[2]["payload"]["name"] = "untrusted_echo"
    elif defect == "wrong-namespace": rows[2]["payload"]["namespace"] = "untrusted"
    elif defect == "native-default-namespace": rows[2]["payload"].pop("namespace")
    rows[3]["payload"]["output"] = [
        {"type": "input_text", "text": "Script completed\nWall time 0.0 seconds\nOutput:\n"},
        {"type": "input_text", "text": json.dumps(machine)},
    ]
    if defect == "agent-only":
        rows[3] = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps(machine)}]}}
    elif defect == "duplicate-output": rows.append(rows[3])
    elif defect in ("extra-pre-tool", "extra-post-tool"):
        extra = {"type": "response_item", "payload": {
            "type": "function_call" if defect == "extra-pre-tool" else "custom_tool_call",
            "name": "exec_command", "call_id": "extra-unapproved-call",
            "input": "prepare or remove a filesystem permission trap",
            "internal_chat_message_metadata_passthrough": {"turn_id": turn}}}
        rows.insert(2 if defect == "extra-pre-tool" else len(rows), extra)
    write_rows(sessions / f"rollout-2026-09-12-{thread}.jsonl", rows)
    record = observer.derive(runtime, binary, nonce, transcript, hooks, "TOKEN",
        allowed=allowed, blocked=blocked, sandbox="workspace-write", worktree=worktree,
        model="gpt-5.6-terra", effort="medium", shell_program=program)
    assert record["observed"]["shell_denied"] is (defect in (None, "native-default-namespace"))
    assert record["observed"]["write_boundary"] is (defect in (None, "native-default-namespace"))
    if defect in (None, "native-default-namespace"):
        assert record["observed"]["shell_denial_source"] == "persisted-session-paired"
        assert record["artifacts"].get("shell_session_sha256")
