"""Generic hook-entry merge/remove and the guard wrappers' unchanged behavior."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("operator_guard_hook_under_test", ROOT / "lib/operator_guard_hook.py")
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


# --- verbatim copy of the pre-generalization implementation (equivalence oracle) ---
def _old_pre_tool_use(document):
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" is not an object')
    entries = hooks.setdefault("PreToolUse", [])
    if not isinstance(entries, list):
        raise ValueError('"hooks.PreToolUse" is not a list')
    return entries


def _old_merge_guard(document, command, previous=None):
    merged = copy.deepcopy(document)
    entries = _old_pre_tool_use(merged)
    current = hook.guard_entry(command)
    if entries.count(current) == 1 and previous in (None, command):
        return merged
    stale = [current] + ([hook.guard_entry(previous)] if previous else [])
    entries[:] = [entry for entry in entries if entry not in stale]
    entries.append(current)
    return merged


def _old_remove_guard(document, command):
    removed = copy.deepcopy(document)
    hooks = removed.get("hooks")
    entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if isinstance(entries, list):
        target = hook.guard_entry(command)
        entries[:] = [entry for entry in entries if entry != target]
    return removed


FOREIGN = {"hooks": [{"type": "command", "command": "echo foreign"}]}
FOREIGN_BASH = {"matcher": "Bash", "hooks": [{"type": "command", "command": "other.sh", "timeout": 5}]}
OTHER_GUARD = hook.guard_entry("sh other-guard.sh")
CMD, OLD_CMD = "bash /a/guard.sh", "bash /b/guard.sh"

DOCUMENTS: list[Any] = [
    {},
    {"theme": "dark"},
    {"hooks": {}},
    {"hooks": {"PreToolUse": []}},
    {"hooks": {"PreToolUse": [FOREIGN]}},
    {"hooks": {"PreToolUse": [FOREIGN, hook.guard_entry(CMD), FOREIGN_BASH]}},
    {"hooks": {"PreToolUse": [hook.guard_entry(CMD), hook.guard_entry(CMD)]}},
    {"hooks": {"PreToolUse": [FOREIGN, hook.guard_entry(OLD_CMD), OTHER_GUARD]}},
    {"zeta": 1, "hooks": {"PostToolUse": [FOREIGN], "PreToolUse": [FOREIGN_BASH]}, "alpha": 2},
    {"hooks": {"PreToolUse": [hook.guard_entry(OLD_CMD), hook.guard_entry(CMD)]}},
    {"hooks": "nope"},
    {"hooks": {"PreToolUse": "nope"}},
]


def _outcome(function, *args):
    try:
        return ("ok", json.dumps(function(*args)))
    except ValueError as exc:
        return ("error", str(exc))


@pytest.mark.parametrize("document", DOCUMENTS)
@pytest.mark.parametrize("previous", [None, OLD_CMD, CMD])
def test_merge_guard_is_byte_identical_to_the_old_implementation(document, previous) -> None:
    before = copy.deepcopy(document)
    assert _outcome(hook.merge_guard, document, CMD, previous) == _outcome(_old_merge_guard, document, CMD, previous)
    assert document == before


@pytest.mark.parametrize("document", DOCUMENTS)
@pytest.mark.parametrize("command", [CMD, OLD_CMD, "bash /missing.sh"])
def test_remove_guard_is_byte_identical_to_the_old_implementation(document, command) -> None:
    before = copy.deepcopy(document)
    assert _outcome(hook.remove_guard, document, command) == _outcome(_old_remove_guard, document, command)
    assert document == before


def test_session_start_entry_shape_and_overrides() -> None:
    assert hook.session_start_entry() == {
        "hooks": [{"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR/.ffs/cloud/session-start.sh"',
                   "timeout": 300}]
    }
    # T2: anchored on the project dir, so a session started in a subdirectory still finds the script
    assert hook.SESSION_START_COMMAND.startswith('bash "$CLAUDE_PROJECT_DIR/')
    assert not hook.SESSION_START_COMMAND.startswith("bash .ffs")
    assert hook.session_start_entry("x.sh", 9) == {"hooks": [{"type": "command", "command": "x.sh", "timeout": 9}]}


def test_merge_hook_entry_creates_missing_keys_and_deep_copies() -> None:
    entry = hook.session_start_entry()
    document: dict[str, Any] = {"model": "opus"}
    merged = hook.merge_hook_entry(document, "SessionStart", entry)
    assert document == {"model": "opus"}
    assert merged == {"model": "opus", "hooks": {"SessionStart": [entry]}}
    merged["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 1
    assert entry["hooks"][0]["timeout"] == 300


def test_merge_hook_entry_is_idempotent_and_keeps_foreign_order() -> None:
    entry = hook.session_start_entry()
    document = {"hooks": {"SessionStart": [FOREIGN, entry, FOREIGN_BASH], "Stop": [FOREIGN]}}
    assert hook.merge_hook_entry(document, "SessionStart", entry) == document
    assert hook.merge_hook_entry(hook.merge_hook_entry(document, "SessionStart", entry), "SessionStart", entry) == document


def test_merge_hook_entry_replaces_previous_and_dedupes() -> None:
    old, new = hook.session_start_entry("bash old.sh"), hook.session_start_entry()
    document = {"hooks": {"SessionStart": [old, FOREIGN, new, new]}}
    merged = hook.merge_hook_entry(document, "SessionStart", new, previous=old)
    assert merged["hooks"]["SessionStart"] == [FOREIGN, new]


def test_merge_hook_entry_owns_only_exact_matches() -> None:
    near = hook.session_start_entry(timeout=301)
    merged = hook.merge_hook_entry({"hooks": {"SessionStart": [near]}}, "SessionStart", hook.session_start_entry())
    assert merged["hooks"]["SessionStart"] == [near, hook.session_start_entry()]


@pytest.mark.parametrize("document", [{"hooks": []}, {"hooks": {"SessionStart": {}}}])
def test_merge_hook_entry_rejects_wrong_shapes(document) -> None:
    with pytest.raises(ValueError):
        hook.merge_hook_entry(document, "SessionStart", hook.session_start_entry())


def test_remove_hook_entry_removes_only_the_exact_entry() -> None:
    entry = hook.session_start_entry()
    document = {"hooks": {"SessionStart": [FOREIGN, entry, hook.session_start_entry(timeout=301)]}}
    removed = hook.remove_hook_entry(document, "SessionStart", entry)
    assert removed["hooks"]["SessionStart"] == [FOREIGN, hook.session_start_entry(timeout=301)]
    assert document["hooks"]["SessionStart"][1] == entry


def test_remove_hook_entry_prunes_emptied_containers_only() -> None:
    entry = hook.session_start_entry()
    assert hook.remove_hook_entry({"a": 1, "hooks": {"SessionStart": [entry]}}, "SessionStart", entry) == {"a": 1}
    assert hook.remove_hook_entry(
        {"hooks": {"SessionStart": [entry], "Stop": [FOREIGN]}}, "SessionStart", entry
    ) == {"hooks": {"Stop": [FOREIGN]}}
    # untouched when nothing was removed, even if empty containers pre-exist
    pre_existing = {"hooks": {"SessionStart": []}}
    assert hook.remove_hook_entry(pre_existing, "SessionStart", entry) == pre_existing
    assert hook.remove_hook_entry({"hooks": {"SessionStart": [entry]}}, "SessionStart", entry, prune=False) == {
        "hooks": {"SessionStart": []}
    }


def test_remove_hook_entry_tolerates_missing_or_odd_shapes() -> None:
    entry = hook.session_start_entry()
    for document in ({}, {"hooks": "x"}, {"hooks": {"SessionStart": "x"}}, {"hooks": {}}):
        assert hook.remove_hook_entry(document, "SessionStart", entry) == document
