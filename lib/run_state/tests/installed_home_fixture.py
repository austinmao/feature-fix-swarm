"""A user-scope FFS install built by the real ``setup.sh`` into a throwaway HOME.

Staging tests start from what the installer actually wrote instead of hand-seeding
hook registrations. Only the upstream-GSD closure, which the stub GSD installer
cannot pin, is completed by the tests themselves.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GUARD = "operator-only-guard.sh"
GUARD_DENY = "python3 lib/gates.py intent-check --spec s.md --repin"
GUARD_ALLOW = "python3 lib/gates.py intent-check --spec s.md"
CLOSED_PATH = "/usr/bin:/bin"


def install_user_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"), "XDG_STATE_HOME": str(home / ".local/state"),
        "FFS_SKIP_PROMPT_MASTER": "1", "FFS_SKIP_SOCRATIC": "1",
        "FFS_GSD_INSTALLER": str(ROOT / "tests/fixtures/gsd-installer-stub.py"),
        "GSD_MODEL_PROBE_CMD": "true", "GSD_MODEL_PROBE_CMD_CODEX": "true",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "setup.sh"), "--scope", "user"], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert result.returncode == 0, result.stderr
    return home


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def guard_command_of(hooks_json: Path) -> str:
    entries = json.loads(hooks_json.read_text())["hooks"]["PreToolUse"]
    (command,) = [hook["command"] for entry in entries if entry["matcher"] == "Bash" for hook in entry["hooks"]]
    return command


def run_guard_command(command: str, tool_input: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a registered hook command exactly as a host would: one JSON line, closed PATH."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": tool_input}) + "\n"
    return subprocess.run(
        command, shell=True, cwd=cwd, input=payload, text=True, env={"PATH": CLOSED_PATH},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
