#!/usr/bin/env python3
"""Lint imperative FFS skill examples for unqualified host syntax."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


CLAUDE_CALL = re.compile(r"(?<![\w$])/[a-z][a-z0-9-]+")
HOST_TOOL = re.compile(r"\b(?:Agent|Skill) tool\b|\bSkill\([^)]")
CLAUDE_ALIAS = re.compile(r"(?<![-\w])(?:haiku|sonnet|opus|fable)(?![-\w])", re.IGNORECASE)


def lint_skill_text(text: str) -> list[str]:
    failures: list[str] = []
    shared_contract = (
        "## Host dispatch contract" in text
        and "Codex:" in text
        and "Claude:" in text
        and "A bare `/skill`" in text
    )
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith("#"):
            continue
        if (
            CLAUDE_CALL.search(line)
            and not shared_contract
            and "Claude:" not in line
            and "Claude `" not in line
        ):
            failures.append(f"line {number}: unqualified Claude skill call")
        if HOST_TOOL.search(line) and not (
            "Claude:" in line or "Codex:" in line or "host dispatch" in line.lower()
        ):
            failures.append(f"line {number}: unqualified host tool")
        if CLAUDE_ALIAS.search(line) and not (
            "Claude" in line
            or "legacy" in line.lower()
            or "kind" in line.lower() and "exact" in line.lower()
            or "fable-agent-orchestration" in line
        ):
            failures.append(f"line {number}: unqualified Claude model alias")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = False
    for path in args.paths:
        for failure in lint_skill_text(path.read_text()):
            print(f"{path}:{failure}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
