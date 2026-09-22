"""Claude credential availability follows authenticated model/tool intervals."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from run_state.native_review_runtime import (
    NativeReviewRequest, prepare_native_review_runtime, verify_claude_review_evidence,
)
from run_state.supervisor import _guarded_claude_exec
from test_native_review_session import _claude_review_evidence


def test_claude_credential_is_absent_during_tool_then_restored_for_next_turn(tmp_path, capfd):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    credential = home / ".credentials.json"
    credential.write_text('{"claudeAiOauth":{"accessToken":"fixture"}}\n')
    credential.chmod(0o600)
    info = credential.stat()
    session = "11111111-1111-4111-8111-111111111111"
    script = """
import json, pathlib, sys, time
credential = pathlib.Path(sys.argv[1])
session = sys.argv[2]
print(json.dumps({"type":"system","subtype":"init","session_id":session,
                  "model":"claude-opus-5","claude_code_version":"2.1.274"}), flush=True)
if not credential.exists():
    raise SystemExit(21)
print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"thinking"}]}}), flush=True)
for _ in range(100):
    if not credential.exists():
        break
    time.sleep(0.01)
else:
    raise SystemExit(22)
print(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","id":"tool-1"}]}}), flush=True)
if credential.exists():
    raise SystemExit(23)
print(json.dumps({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tool-1"}]}}), flush=True)
for _ in range(100):
    if credential.exists():
        break
    time.sleep(0.01)
else:
    raise SystemExit(24)
print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}), flush=True)
raise SystemExit(0)
"""
    guard = {
        "path": str(credential),
        "sha256": hashlib.sha256(credential.read_bytes()).hexdigest(),
        "device": info.st_dev,
        "inode": info.st_ino,
        "session_id": session,
        "model": "claude-opus-5",
        "version": "2.1.274",
    }
    environment = {"HOME": str(home.parent), "CLAUDE_CONFIG_DIR": str(home)}
    assert _guarded_claude_exec(
        [sys.executable, "-c", script, str(credential), session], environment, guard,
    ) == 0
    assert not credential.exists()
    records = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    assert [record["type"] for record in records] == [
        "system", "assistant", "assistant", "user", "assistant",
    ]


def test_native_review_material_runs_through_existing_credential_guard(tmp_path, capfd, monkeypatch):
    """Scripted Python executable and dummy credential; no native model/auth proof."""
    initial, records = _claude_review_evidence(tmp_path)
    binary = Path(initial.binary)
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys, time
args = sys.argv[1:]
assert args[args.index('--tools') + 1] == ''
assert args[args.index('--setting-sources') + 1] == ''
assert args[args.index('--session-id') + 1] == '643b3a28-33d2-4000-b983-a18c5da41bbd'
home = pathlib.Path(os.environ['HOME'])
profile = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
assert profile.parent == home and profile != home
credential = profile / '.credentials.json'
assert credential.is_file()
''' + "records = " + repr(records) + '''
assert str(pathlib.Path.cwd()) == records[0]['cwd']
print(json.dumps(records[0]), flush=True)
print(json.dumps(records[1]), flush=True)
for _ in range(100):
    if not credential.exists():
        break
    time.sleep(0.01)
else:
    raise SystemExit(22)
print(json.dumps(records[2]), flush=True)
''')
    request = NativeReviewRequest(
        host="claude", requested_model=initial.requested_model, cli_version=initial.cli_version,
        binary=str(binary), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        runtime_identity=initial.runtime_identity, prompt=initial.argv[-1], session_id=initial.session_id,
    )
    material = prepare_native_review_runtime(
        request, runtime_root=Path(initial.runtime_root).with_name("guarded"),
        workspace=Path(initial.workspace),
    )
    environment = dict(material.environment)
    credential = Path(environment['CLAUDE_CONFIG_DIR']) / '.credentials.json'
    credential.write_text('{"claudeAiOauth":{"accessToken":"fixture"}}\n')
    credential.chmod(0o600)
    info = credential.stat()
    guard = {"path": str(credential), "sha256": hashlib.sha256(credential.read_bytes()).hexdigest(),
             "device": info.st_dev, "inode": info.st_ino, "session_id": material.session_id,
             "model": material.requested_model, "version": material.cli_version}
    monkeypatch.chdir(material.workspace)
    exit_code = _guarded_claude_exec(list(material.argv), environment, guard)
    assert exit_code == 0 and not credential.exists()
    stream = capfd.readouterr().out.encode()
    observation = verify_claude_review_evidence(material, stream, exit_code=exit_code)
    assert observation.telemetry.session_id == material.session_id
    assert observation.telemetry.sha256 == hashlib.sha256(stream).hexdigest()
    assert observation.telemetry.effective_model == material.requested_model
