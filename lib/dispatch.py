"""
Shared task-parsing and model-resolution utilities for /swarm and /feature-implement.

Usage (inline heredoc in SKILL.md):
    TASKS_JSON=$(FILE="$TASKS_FILE" python3 lib/dispatch.py parse)
    RESOLVED=$(python3 lib/dispatch.py resolve --model opus --thinking med)
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional


# ── Patterns ─────────────────────────────────────────────────────────────────

TASK_PATTERN = re.compile(
    r'^- \[([ XxFfSs])\] ([^\n]+)$(?:\n[ ]{2,}Depends-on:\s*([^\n]*))?',
    re.MULTILINE,
)
PHASE_PATTERN = re.compile(r'^## Phase [^\n]*$', re.MULTILINE)

STATUS_MAP = {
    ' ': 'todo', 'X': 'done', 'x': 'done',
    'F': 'failed', 'f': 'failed', 'S': 'skipped', 's': 'skipped',
}

COST_PER_TASK = {"haiku": 0.02, "sonnet": 0.30, "opus": 2.00, "fable": 0.50}

THINKING_GUIDANCE = {
    "low":  "Respond directly. No extended reasoning needed. Be concise.",
    "med":  "Think through your approach before executing. One pass of analysis, then act.",
    "high": "Explore alternatives. Consider edge cases. Verify assumptions before acting.",
    "max":  "Deep analysis required. Examine all tradeoffs. Verify security and correctness exhaustively.",
}


# ── Core functions ────────────────────────────────────────────────────────────

def phase_for(content: str, pos: int, phase_starts: list) -> str:
    last = None
    for start, header in phase_starts:
        if start <= pos:
            last = header
        else:
            break
    return last or "(no phase)"


def parse_annotations(rest: str, default_agent: str = "general-purpose") -> dict:
    """Extract [model:X] [thinking:Y] [agent:Z] [P] [USn] [qa:dims] from annotation string."""
    parallel = bool(re.search(r'\[P\]', rest))

    us_match = re.search(r'\[US(\d+)\]', rest)
    user_story = f"US{us_match.group(1)}" if us_match else None

    model, thinking = "sonnet", "med"
    m = re.search(r'\[model:([a-z]+)(?:\s+thinking:([a-z]+))?\]', rest)
    if m:
        model = m.group(1)
        if m.group(2):
            thinking = m.group(2)
    m2 = re.search(r'\[thinking:([a-z]+)\]', rest)
    if m2:
        thinking = m2.group(1)

    agent = default_agent
    m3 = re.search(r'\[agent:([^\]]+)\]', rest)
    if m3:
        agent = m3.group(1)

    qa_match = re.search(r'\[qa:([a-z,]+)\]', rest)
    qa_dims = qa_match.group(1).split(",") if qa_match else ["e2e", "review", "security"]

    strip_pat = r'\[(?:P|US\d+|model:[^\]]+|thinking:[^\]]+|agent:[^\]]+|qa:[^\]]+)\]'
    desc = re.sub(strip_pat, '', rest).strip()

    return {
        "parallel": parallel,
        "user_story": user_story,
        "model": model,
        "thinking": thinking,
        "agent": agent,
        "qa_dims": qa_dims,
        "description": desc,
    }


def parse_tasks_md(content: str, default_agent: str = "general-purpose") -> list[dict]:
    """Parse tasks.md content into structured task list."""
    phase_starts = [(m.start(), m.group()) for m in PHASE_PATTERN.finditer(content)]

    tasks = []
    for m in TASK_PATTERN.finditer(content):
        rest = m.group(2)
        deps_raw = m.group(3) or ""
        deps = [d.strip() for d in deps_raw.split(",") if d.strip().startswith("T")]
        # T-number may appear anywhere in the line (before or after annotations)
        tid_match = re.search(r'\bT(\d{3,})\b', rest)
        if not tid_match:
            continue
        task_id = f"T{tid_match.group(1)}"
        ann = parse_annotations(rest, default_agent=default_agent)
        tasks.append({
            "id": task_id,
            "status": STATUS_MAP.get(m.group(1), "todo"),
            "phase": phase_for(content, m.start(), phase_starts),
            "depends_on": deps,
            **ann,
        })
    return tasks


def resolve_thinking(model: str, thinking: str) -> str:
    """Align thinking budget with model tier to avoid waste or under-utilization."""
    if model == "opus" and thinking == "med":
        return "high"   # opus + med wastes capability
    if model == "haiku" and thinking in ("high", "max"):
        return "med"    # haiku can't utilize max budget
    return thinking


def estimate_cost(tasks: list[dict]) -> float:
    """Rough USD estimate based on model tier counts."""
    return sum(COST_PER_TASK.get(t["model"], 0.30) for t in tasks)


def fable_ruflo_warn(tasks: list[dict]) -> list[str]:
    """Return task IDs with [model:fable] that will silently downgrade to sonnet on Ruflo path."""
    return [t["id"] for t in tasks if t.get("model") == "fable"]


# ── CLI entry point ───────────────────────────────────────────────────────────

def cmd_parse(args: list[str]) -> None:
    """Parse FILE env var → emit JSON task list to stdout."""
    path = os.environ.get("FILE")
    if not path:
        print("ERROR: FILE env var required", file=sys.stderr)
        sys.exit(1)
    default_agent = args[0] if args else "general-purpose"
    with open(path) as f:
        content = f.read()
    tasks = parse_tasks_md(content, default_agent=default_agent)
    print(json.dumps(tasks))


def cmd_resolve(args: list[str]) -> None:
    """resolve --model M --thinking T → emit adjusted thinking to stdout."""
    model = "sonnet"
    thinking = "med"
    i = 0
    while i < len(args):
        if args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]; i += 2
        elif args[i] == "--thinking" and i + 1 < len(args):
            thinking = args[i + 1]; i += 2
        else:
            i += 1
    print(resolve_thinking(model, thinking))


def cmd_cost(args: list[str]) -> None:
    """cost FILE → emit float cost estimate to stdout."""
    path = args[0] if args else os.environ.get("FILE")
    if not path:
        print("ERROR: pass file path or set FILE env var", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        content = f.read()
    tasks = parse_tasks_md(content)
    print(f"{estimate_cost(tasks):.2f}")


def cmd_fable_warn(args: list[str]) -> None:
    """fable-warn FILE → print WARN line for any [model:fable] tasks (Ruflo downgrades to sonnet)."""
    path = args[0] if args else os.environ.get("FILE")
    if not path:
        print("ERROR: pass file path or set FILE env var", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        content = f.read()
    tasks = parse_tasks_md(content)
    ids = fable_ruflo_warn(tasks)
    if ids:
        print(f"WARN: [model:fable] tasks {ids} will downgrade to sonnet on Ruflo path "
              f"(Ruflo enum: haiku|sonnet|opus|inherit only). "
              f"Use RUFLO_REQUIRED=0 for native Agent path to preserve fable routing.")


COMMANDS = {
    "parse": cmd_parse,
    "resolve": cmd_resolve,
    "cost": cmd_cost,
    "fable-warn": cmd_fable_warn,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: python3 lib/dispatch.py <{' | '.join(COMMANDS)}>", file=sys.stderr)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
