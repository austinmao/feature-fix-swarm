#!/usr/bin/env python3
"""External-host mock for the controlled Codex runtime observer.

It models only the CLI boundary: the runner invokes this executable with the
staged runtime and can verify output, hooks, and filesystem effects itself.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def emit(record: dict) -> None:
    print(json.dumps(record, separators=(",", ":")))


def complete(item: dict) -> None:
    emit({"type": "item.completed", "item": item})


def _argument(argv: list[str], key: str, default: str = "") -> str:
    try:
        return argv[argv.index(key) + 1]
    except (ValueError, IndexError):
        return default


def _config(argv: list[str], name: str) -> str:
    prefix = name + '="'
    for index, argument in enumerate(argv[:-1]):
        if argument == "-c" and argv[index + 1].startswith(prefix) and argv[index + 1].endswith('"'):
            return argv[index + 1][len(prefix):-1]
    return ""


def _native_session(home: Path, program: str, argv: list[str], *, positive: bool,
                    shell_result: dict | None = None) -> tuple[str, str]:
    """Write a hermetic CLI simulation session, never evidence of live web access."""
    digest = hashlib.sha256(("positive" if positive else "negative").encode() + program.encode()).hexdigest()
    thread_id, turn_id, call_id = f"fixture-{digest[:24]}", f"turn-{digest[24:48]}", f"call-{digest[48:64]}"
    nonce = re.search(r'nonce:"([^"]+)"', program)
    nonce_value = nonce.group(1) if nonce else ""
    cwd, model, effort = _argument(argv, "--cd"), _config(argv, "model"), _config(argv, "model_reasoning_effort")
    machine = shell_result if shell_result is not None else (
        {"nonce": nonce_value, "available": True} if positive else {"nonce": nonce_value, "attempt": True})
    output = [
        {"type": "input_text", "text": "Script completed\n" if positive else "Script failed\n"},
        {"type": "input_text", "text": json.dumps(machine, separators=(",", ":"))},
    ]
    if not positive:
        output.append({"type": "input_text", "text": "\nTypeError: tools.web__run is not a function\n"})
    rows = [
        {"type": "session_meta", "payload": {"id": thread_id, "session_id": thread_id, "cwd": cwd}},
        {"type": "turn_context", "payload": {"turn_id": turn_id, "cwd": cwd, "model": model, "effort": effort}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "namespace": "functions", "call_id": call_id, "input": program, "internal_chat_message_metadata_passthrough": {"turn_id": turn_id}}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": output, "internal_chat_message_metadata_passthrough": {"turn_id": turn_id}}},
    ]
    sessions = home / "sessions" / "fixture"; sessions.mkdir(parents=True, exist_ok=True)
    session = sessions / f"rollout-hermetic-{thread_id}.jsonl"
    session.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    session.chmod(0o600)
    return thread_id, "Script completed" if positive else "Script failed"


def main(argv: list[str]) -> int:
    prompt = argv[-1] if argv else ""
    home = Path(os.environ.get("CODEX_HOME", ""))
    native_prefix = "Call functions.exec exactly once with the following program and do not use any other tool: "
    if prompt.startswith(native_prefix):
        # Hermetic CLI simulation only: this models the persisted Codex
        # session schema expected by the observer.  It is never live proof.
        program = prompt[len(native_prefix):]
        positive = 'web_search="live"' in argv
        thread_id, status = _native_session(home, program, argv, positive=positive)
        emit({"type": "thread.started", "thread_id": thread_id})
        complete({"type": "agent_message", "text": status})
        emit({"type": "turn.completed"})
        return 0

    # The token must come from the actual staged trusted skill, never from
    # observer prompt text.  This is the external-host analogue of Codex
    # skill discovery.
    skill = home / "skills" / "ffs-observer" / "SKILL.md"
    token = re.search(r"FFS_OBSERVER_SKILL_[0-9a-f]+", skill.read_text(encoding="utf-8")) if skill.is_file() else None
    shell_program, shell_command = "", ""
    marker = "const command="
    if marker in prompt:
        shell_program = marker + prompt.split(marker, 1)[1].split(". Report the tool result", 1)[0]
        shell_command, _ = json.JSONDecoder().raw_decode(shell_program[len(marker):])
    encoded_paths = re.findall(r'b64decode\("([A-Za-z0-9+/=]+)"\)\.decode\(\)', shell_command or prompt)
    runtime_probe = shell_command == 'python3 "$CODEX_HOME/.ffs-observer-shell-probe.py"'
    if not token or (not runtime_probe and len(encoded_paths) != 2):
        return 64
    if runtime_probe:
        nonce = os.environ.get("FFS_HOOK_NONCE", "")
        workspace = Path(_argument(argv, "--cd"))
        allowed_path = workspace / f"ffs-observer-allowed-{nonce}.txt"
        admission = os.environ.get("FFS_SUPERVISED_ADMISSION_FILE")
        blocked_path = Path((admission or str(home / "supervisor-admission.json")) + f".blocked-{nonce}")
    else:
        allowed_path, blocked_path = (Path(os.fsdecode(base64.b64decode(item))) for item in encoded_paths)
    allowed_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox = argv[argv.index("--sandbox") + 1] if "--sandbox" in argv else "workspace-write"
    if sandbox != "read-only":
        allowed_path.write_text("allowed", encoding="utf-8")
    if sandbox == "danger-full-access":
        blocked_path.write_text("outside allowed under granted policy", encoding="utf-8")
    else:
        blocked_path.unlink(missing_ok=True)

    hooks, nonce = os.environ.get("FFS_HOOK_OBSERVATION"), os.environ.get("FFS_HOOK_NONCE")
    if hooks and nonce:
        with Path(hooks).open("a", encoding="utf-8") as stream:
            for event in ("SessionStart", "Stop", "PostToolUse", "PreToolUse", "UserPromptSubmit"):
                stream.write(f"{nonce} {event}\n")

    thread_id = None
    if shell_program:
        thread_id, _ = _native_session(home, shell_program, argv, positive=True,
            shell_result={"nonce": nonce, "command": shell_command,
                          "exit_code": 0 if sandbox == "danger-full-access" else 1,
                          "output": "writes succeeded" if sandbox == "danger-full-access"
                          else "PermissionError: outside writable root"})
    emit({"type": "thread.started", **({"thread_id": thread_id} if thread_id else {})})
    complete({"type": "agent_message", "text": token.group(0)})
    complete({
        "type": "command_execution",
        "command": f"write {allowed_path} then {blocked_path}",
        "exit_code": 0 if sandbox == "danger-full-access" else 1,
        "aggregated_output": "writes succeeded" if sandbox == "danger-full-access" else "PermissionError: outside writable root",
    })
    emit({"type": "turn.completed"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
