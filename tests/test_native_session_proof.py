"""Independent raw-session boundary tests; hermetic records are not live canaries."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_session_observer", ROOT / "scripts/gsd/codex-runtime-observer.py")
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)


@pytest.mark.parametrize("defect", [None, "native-default-namespace", "agent-only", "wrong-call", "wrong-turn", "wrong-thread",
                                    "wrong-model", "wrong-effort", "wrong-workspace", "replayed-nonce",
                                    "spoofed-program", "wrong-tool", "wrong-namespace", "duplicate-output", "symlink-rollout", "truncated", "extra-tool"])
def test_native_denial_requires_bound_machine_call_and_exact_probe(tmp_path: Path, defect: str | None) -> None:
    runtime = tmp_path / "runtime"
    sessions = runtime / "sessions" / "2026" / "09" / "12"
    sessions.mkdir(parents=True, mode=0o700)
    worktree = tmp_path / "worktree"
    worktree.mkdir(mode=0o700)
    nonce = "acceptance-native-nonce"
    transcripts = []
    for mode in ("positive", "negative"):
        thread_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "ffs-native-thread-" + mode))
        turn_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "ffs-native-turn-" + mode))
        call_id = f"call_fixture_{mode}"
        transcript = runtime / f"{mode}.jsonl"
        write_rows(transcript, [{"type": "thread.started", "thread_id": thread_id},
                                {"type": "turn.completed"}])
        transcripts.append(transcript)
        source_nonce = "old-nonce" if defect == "replayed-nonce" and mode == "negative" else nonce
        code = ("text({nonce:" + json.dumps(source_nonce) + ",available:typeof tools.web__run==='function'})"
                if mode == "positive" else
                "text({nonce:" + json.dumps(source_nonce) + ",attempt:true});await tools.web__run({time:[{utc_offset:'+00:00'}]})")
        output = ([{"type": "input_text", "text": "Script completed\nWall time 0.0 seconds\nOutput:\n"},
                   {"type": "input_text", "text": json.dumps({"nonce": nonce, "available": True})}]
                  if mode == "positive" else
                  [{"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
                   {"type": "input_text", "text": json.dumps({"nonce": nonce, "attempt": True})},
                   {"type": "input_text", "text": "Script error:\nTypeError: tools.web__run is not a function\n    at exec_main.mjs:1:20"}])
        rows = [
            {"type": "session_meta", "payload": {"id": thread_id, "session_id": thread_id,
                                                  "cwd": str(worktree), "model_provider": "openai"}},
            {"type": "turn_context", "payload": {"turn_id": turn_id, "cwd": str(worktree),
                                                   "model": "gpt-5.6-terra", "effort": "medium"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                                                    "namespace": "functions", "call_id": call_id, "input": code,
                                                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id}}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                                                    "output": output, "internal_chat_message_metadata_passthrough": {"turn_id": turn_id}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}},
        ]
        if mode == "negative":
            if defect == "agent-only":
                rows[3] = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                                "content": [{"type": "output_text", "text": json.dumps(output)}]}}
            elif defect == "wrong-call": rows[3]["payload"]["call_id"] = "another-call"
            elif defect == "wrong-turn": rows[3]["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = "another-turn"
            elif defect == "wrong-thread": rows[0]["payload"]["id"] = "another-thread"
            elif defect == "wrong-model": rows[1]["payload"]["model"] = "gpt-5.6-sol"
            elif defect == "wrong-effort": rows[1]["payload"]["effort"] = "high"
            elif defect == "wrong-workspace": rows[1]["payload"]["cwd"] = str(tmp_path / "sibling")
            elif defect == "spoofed-program": rows[2]["payload"]["input"] = "text('TypeError: tools.web__run is not a function')"
            elif defect == "wrong-tool": rows[2]["payload"]["name"] = "untrusted_echo"
            elif defect == "wrong-namespace": rows[2]["payload"]["namespace"] = "untrusted"
            elif defect == "native-default-namespace": rows[2]["payload"].pop("namespace")
            elif defect == "duplicate-output": rows.insert(4, rows[3])
            elif defect == "extra-tool":
                rows.insert(2, {"type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "extra-unapproved-call",
                    "arguments": "alter the native tool environment",
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id}}})
        rollout = sessions / f"rollout-2026-09-12-{thread_id}.jsonl"
        write_rows(rollout, rows)
        if mode == "negative" and defect == "symlink-rollout":
            outside = tmp_path / "outside-rollout"
            rollout.rename(outside)
            rollout.symlink_to(outside)
        if mode == "negative" and defect == "truncated":
            rollout.write_text(rollout.read_text() + '{"type":')

    proved, artifacts = observer._native_proof(
        *transcripts, runtime=runtime, nonce=nonce, worktree=worktree,
        model="gpt-5.6-terra", effort="medium",
    )
    assert proved is (defect in (None, "native-default-namespace"))
    if defect in (None, "native-default-namespace"):
        assert artifacts, "Accepted machine proof must retain its source artifacts"
