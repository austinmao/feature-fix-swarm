#!/usr/bin/env python3
"""Reject raw model-selection surfaces outside the typed FFS resolver."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


RUNTIME_CONTRACTS = {
    "plan-adversary.sh": "PLAN_ADVERSARY_MODEL_REQUEST",
    "qa-coverage-adversary.sh": "QA_COVERAGE_MODEL_REQUEST",
    "review-gate-command.sh": "GSD_REVIEW_MODEL_REQUEST",
    "scope-drift-gate.sh": "GSD_DRIFT_MODEL_REQUEST",
}
LEGACY_EXPANSION = re.compile(
    r"\$\{(?:PLAN_ADVERSARY_(?:MODEL|EFFORT|CLAUDE_MODEL)|"
    r"QA_COVERAGE_(?:MODEL|EFFORT|CLAUDE_MODEL)|"
    r"GSD_REVIEW_(?:MODEL|EFFORT|CLAUDE_MODEL)|"
    r"GSD_DRIFT_(?:MODEL|EFFORT|CLAUDE_MODEL))(?:[:}?])"
)
RAW_SKILL_ASSIGNMENT = re.compile(
    r"\b[A-Z][A-Z0-9_]*MODEL\s*=\s*[\"'](?:gpt-|claude-|opus[\"']|sonnet[\"']|haiku[\"']|fable[\"'])",
    re.IGNORECASE,
)


def lint(root: Path) -> list[str]:
    errors: list[str] = []
    runtime_root = root / "scripts/gsd"
    for filename, request_variable in RUNTIME_CONTRACTS.items():
        path = runtime_root / filename
        if not path.is_file():
            errors.append(f"{path}: missing runtime model-routing surface")
            continue
        source = path.read_text()
        if request_variable not in source:
            errors.append(f"{path}: missing typed request variable {request_variable}")
        if "adversary_invoke_typed_request" not in source:
            errors.append(f"{path}: bypasses adversary_invoke_typed_request")
        if LEGACY_EXPANSION.search(source):
            errors.append(f"{path}: expands a retired raw model variable")

    for path in sorted((root / "skills").glob("*/SKILL.md")):
        source = path.read_text()
        if "adversary_invoke_with_fallback" in source:
            errors.append(f"{path}: skill bypasses the typed adversary dispatcher")
        if RAW_SKILL_ASSIGNMENT.search(source):
            errors.append(f"{path}: skill assigns a raw vendor model ID")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    errors = lint(args.root.resolve())
    for error in errors:
        print(f"model-routing-lint: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
