"""Tests for scripts/hooks/run-state-session.py — CLAUDE_SESSION_ID sanitization.

codex-gate Pass 3 #2 CRITICAL: session.env is sourced by shell skills.
Unsafe values (newlines, $(...), `...`, ;rm) would execute on source.
"""
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[3] / "scripts" / "hooks" / "run-state-session.py"


def _run_hook(session_id, home_dir):
    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    if session_id is not None:
        env["CLAUDE_SESSION_ID"] = session_id
    else:
        env.pop("CLAUDE_SESSION_ID", None)
    return subprocess.run([sys.executable, str(HOOK)], capture_output=True, text=True, env=env)


def test_no_session_id_is_silent_noop(tmp_path: Path) -> None:
    r = _run_hook(None, tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / ".claude" / "state" / "session.env").exists()


def test_safe_uuid_session_id_writes_file(tmp_path: Path) -> None:
    r = _run_hook("abc123-def456_789", tmp_path)
    assert r.returncode == 0
    env_file = tmp_path / ".claude" / "state" / "session.env"
    assert env_file.exists()
    assert env_file.read_text() == "CLAUDE_SESSION_ID=abc123-def456_789\n"


def test_newline_injection_rejected(tmp_path: Path) -> None:
    r = _run_hook("abc\nRM=rm", tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / ".claude" / "state" / "session.env").exists()
    assert "rejecting unsafe" in r.stderr


def test_command_substitution_rejected(tmp_path: Path) -> None:
    r = _run_hook("abc$(whoami)", tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / ".claude" / "state" / "session.env").exists()


def test_backtick_injection_rejected(tmp_path: Path) -> None:
    r = _run_hook("abc`whoami`", tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / ".claude" / "state" / "session.env").exists()


def test_semicolon_injection_rejected(tmp_path: Path) -> None:
    r = _run_hook("abc;rm -rf /", tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / ".claude" / "state" / "session.env").exists()


def test_excessively_long_rejected(tmp_path: Path) -> None:
    r = _run_hook("a" * 129, tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / ".claude" / "state" / "session.env").exists()
