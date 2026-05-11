#!/usr/bin/env python3
"""SessionStart hook: write CLAUDE_SESSION_ID to ~/.claude/state/session.env

Bash subprocesses inside skills can `source` this file to learn the current
session id. Hook is silent unless CLAUDE_SESSION_ID is set AND passes a
strict character allowlist (Claude session ids are UUID-shaped).

Security (codex-gate Pass 3 #2 CRITICAL): session.env is sourced by
downstream shell skills. An unsanitized CLAUDE_SESSION_ID containing
newlines, semicolons, backticks, or `$(...)` would execute arbitrary
shell code on `source`. Allowlist is `[A-Za-z0-9_-]+`.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def main() -> int:
    session_id = os.environ.get("CLAUDE_SESSION_ID", "")
    if not session_id:
        return 0
    if not _SAFE_ID.match(session_id):
        # Silent reject — log to stderr but never write the file (and never
        # error-exit, because that would block session start).
        sys.stderr.write("run-state-session: rejecting unsafe CLAUDE_SESSION_ID\n")
        return 0
    state_dir = Path.home() / ".claude" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env_file = state_dir / "session.env"
    env_file.write_text(f"CLAUDE_SESSION_ID={session_id}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
