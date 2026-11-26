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


SESSION_START_COMMAND = 'bash "$CLAUDE_PROJECT_DIR/.ffs/cloud/session-start.sh"'
SESSION_START_TIMEOUT_SECONDS = 300


def session_start_entry(
    command: str = SESSION_START_COMMAND, timeout: int = SESSION_START_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """The committed SessionStart entry ``cloud-dispatch.sh env init --register-hook`` writes."""
    return {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}


def _event_entries(document: dict[str, Any], event: str) -> list[Any]:
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" is not an object')
    entries = hooks.setdefault(event, [])
    if not isinstance(entries, list):
        raise ValueError(f'"hooks.{event}" is not a list')
    return entries


def merge_hook_entry(
    document: dict[str, Any],
    event: str,
    entry: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of ``document`` with exactly one ``entry`` under ``hooks.<event>``.

    Ownership is the exact entry value: an entry is ours iff it equals ``entry``
    (or ``previous``, the entry recorded by an earlier write, which is replaced).
    Foreign entries and every other key are untouched and keep their order; a new
    entry is appended. Missing ``hooks`` / event keys are created.
    """
    merged = copy.deepcopy(document)
    entries = _event_entries(merged, event)
    current = copy.deepcopy(entry)
    if entries.count(current) == 1 and previous in (None, current):
        return merged
    stale = [current] + ([copy.deepcopy(previous)] if previous else [])
    entries[:] = [item for item in entries if item not in stale]
    entries.append(current)
    return merged


def remove_hook_entry(
    document: dict[str, Any], event: str, entry: dict[str, Any], *, prune: bool = True
) -> dict[str, Any]:
    """Return a copy of ``document`` without the exact ``entry`` under ``hooks.<event>``.

    When removing the entry empties the event list (and then ``hooks``), the empty
    containers are dropped too; ``prune=False`` keeps them. A list that was already
    empty, or that still holds foreign entries, is never touched.
    """
    removed = copy.deepcopy(document)
    hooks = removed.get("hooks")
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return removed
    kept = [item for item in entries if item != entry]
    if len(kept) == len(entries):
        return removed
    entries[:] = kept
    if prune and not entries:
        del hooks[event]
        if not hooks:
            del removed["hooks"]
    return removed


def merge_guard(document: dict[str, Any], command: str, previous: str | None = None) -> dict[str, Any]:
    """Return ``document`` with exactly one FFS guard entry for ``command``.

    Ownership is the exact entry value, so foreign entries (even other
    ``operator-only-guard.sh`` registrations) and every other key are untouched.
    ``previous`` is the command recorded by the last install, replaced if it moved.
    """
    return merge_hook_entry(
        document, "PreToolUse", guard_entry(command), guard_entry(previous) if previous else None
    )


def remove_guard(document: dict[str, Any], command: str) -> dict[str, Any]:
    """Return ``document`` without the exact entry ``merge_guard`` wrote for ``command``."""
    return remove_hook_entry(document, "PreToolUse", guard_entry(command), prune=False)
