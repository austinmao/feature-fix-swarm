"""Production Codex auth containment at the supervisor dispatch boundary."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

from run_state.supervisor import _guarded_codex_exec


def _auth_snapshot(path: Path) -> tuple[bytes, int, int, int]:
    info = path.stat()
    return path.read_bytes(), info.st_ino, info.st_mode & 0o777, info.st_nlink


def test_guarded_codex_revokes_only_private_runtime_auth_after_thread_started(
    tmp_path: Path, capfd,
) -> None:
    private_runtime = tmp_path / "private-runtime"
    template_profile = tmp_path / "template-profile"
    active_profile = tmp_path / "active-profile"
    for profile, contents in (
        (private_runtime, b'{"profile":"private"}\n'),
        (template_profile, b'{"profile":"template"}\n'),
        (active_profile, b'{"profile":"active"}\n'),
    ):
        profile.mkdir(mode=0o700)
        auth = profile / "auth.json"
        auth.write_bytes(contents)
        auth.chmod(0o600)

    independent_before = {
        path: _auth_snapshot(path / "auth.json")
        for path in (template_profile, active_profile)
    }
    private_auth = private_runtime / "auth.json"
    private_info = private_auth.stat()
    private_digest = hashlib.sha256(private_auth.read_bytes()).hexdigest()

    fake_codex = tmp_path / "codex-after-thread-started.py"
    fake_codex.write_text(
        "import json, os, time\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'containment-test'}), flush=True)\n"
        "time.sleep(0.2)\n"
        "try:\n"
        "    open(os.path.join(os.environ['CODEX_HOME'], 'auth.json'), 'rb').read()\n"
        "    auth_after_event = 'read'\n"
        "except FileNotFoundError:\n"
        "    auth_after_event = 'missing'\n"
        "print(json.dumps({'type': 'auth.read.after.thread.started', 'result': auth_after_event}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {\n"
        "    'input_tokens': 1, 'cached_input_tokens': 0,\n"
        "    'cache_write_input_tokens': 0, 'output_tokens': 1,\n"
        "    'reasoning_output_tokens': 0\n"
        "}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)

    environment = {
        "HOME": str(private_runtime),
        "CODEX_HOME": str(private_runtime),
        "PATH": os.defpath,
    }
    guard = {
        "path": str(private_auth),
        "sha256": private_digest,
        "device": private_info.st_dev,
        "inode": private_info.st_ino,
    }

    assert _guarded_codex_exec([sys.executable, str(fake_codex)], environment, guard) == 0

    records = [json.loads(line) for line in capfd.readouterr().out.splitlines()]
    assert records[0]["type"] == "thread.started"
    assert records[1] == {"type": "auth.read.after.thread.started", "result": "missing"}
    assert records[2]["type"] == "turn.completed"
    assert not private_auth.exists()
    for profile, before in independent_before.items():
        assert _auth_snapshot(profile / "auth.json") == before
