#!/usr/bin/env python3
"""SessionStart hook: write CLAUDE_SESSION_ID to ~/.claude/state/session.env

Bash subprocesses inside skills can `source` this file to learn the current
session id. Hook is silent unless CLAUDE_SESSION_ID is set in env.
"""
from __future__ import annotations

import os
from pathlib import Path


def main() -> int:
    session_id = os.environ.get("CLAUDE_SESSION_ID", "")
    if not session_id:
        return 0
    state_dir = Path.home() / ".claude" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env_file = state_dir / "session.env"
    env_file.write_text(f"CLAUDE_SESSION_ID={session_id}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
