"""The one definition of the FFS operator-only guard's hook registration.

The installer (user and project scope) and the headless Codex bundle stager all
build the same PreToolUse entry from here, so its shape cannot drift between
runtimes. Standard library only: scripts that run outside the managed lib
import this file by path.
"""

from __future__ import annotations

import copy
import shlex
from pathlib import Path
from typing import Any

GUARD_NAME = "operator-only-guard.sh"
GUARD_TIMEOUT_SECONDS = 10


def guard_shell_command(script_expr: str) -> str:
    """Run the guard at a shell-expression path; a missing script is a silent no-op.

    A removed or half-uninstalled copy must not turn every Bash call into a hook error.
    """
    return f"""sh -c '[ ! -f "$0" ] || exec bash "$0"' {script_expr}"""


def guard_command(script: str | Path) -> str:
    return guard_shell_command(shlex.quote(str(script)))


def guard_entry(command: str) -> dict[str, Any]:
    return {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": command, "timeout": GUARD_TIMEOUT_SECONDS}],
    }


def _pre_tool_use(document: dict[str, Any]) -> list[Any]:
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" is not an object')
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        raise ValueError('"hooks.PreToolUse" is not a list')
    return entries


def merge_guard(document: dict[str, Any], command: str, previous: str | None = None) -> dict[str, Any]:
    """Return ``document`` with exactly one FFS guard entry for ``command``.

    Ownership is the exact entry value, so foreign entries (even other
    ``operator-only-guard.sh`` registrations) and every other key are untouched.
    ``previous`` is the command recorded by the last install, replaced if it moved.
    """
    merged = copy.deepcopy(document)
    entries = _pre_tool_use(merged)
    current = guard_entry(command)
    if entries.count(current) == 1 and previous in (None, command):
        return merged
    stale = [current] + ([guard_entry(previous)] if previous else [])
    entries[:] = [entry for entry in entries if entry not in stale]
    entries.append(current)
    return merged


def remove_guard(document: dict[str, Any], command: str) -> dict[str, Any]:
    """Return ``document`` without the exact entry ``merge_guard`` wrote for ``command``."""
    removed = copy.deepcopy(document)
    hooks = removed.get("hooks")
    entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if isinstance(entries, list):
        target = guard_entry(command)
        entries[:] = [entry for entry in entries if entry != target]
    return removed
