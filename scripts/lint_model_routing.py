#!/usr/bin/env python3
"""Reject raw model-selection surfaces outside the typed FFS resolver."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


RUNTIME_CONTRACTS = {
    "plan-adversary.sh": "PLAN_ADVERSARY_MODEL_REQUEST",
    "qa-coverage-adversary.sh": "QA_COVERAGE_MODEL_REQUEST",
    "review-gate-command.sh": "GSD_REVIEW_MODEL_REQUEST",
    "scope-drift-gate.sh": "GSD_DRIFT_MODEL_REQUEST",
}

# One source of truth (spec-004 §One source of truth): canonical tiers + the
# alias each tier resolves to in the hand-maintained projections below.
CANONICAL_TIERS = ("frontier", "judgment", "execution", "volume")
TIER_ALIAS = {"frontier": "fable", "judgment": "opus", "execution": "sonnet", "volume": "haiku"}
KNOWN_ALIASES = set(TIER_ALIAS.values())
# Roles intentionally absent from model_overrides: neither is a gsd-core
# subagent type that delegation-enforcer.sh pins from model_overrides.
ROLE_ALLOWLIST = {"orchestrator", "spec-status"}
TIER_MODELS_EXPECTED = {"light": "haiku", "standard": "sonnet", "heavy": "opus"}
HARDCODED_CONSUMERS = {
    "scripts/gsd/gsd-run.sh": re.compile(r"tier = \{(.*?)\}\.get\(name, model\)", re.S),
    "lib/model_eval.py": re.compile(r'result\["tier"\] not in \{(.*?)\}', re.S),
    "scripts/run-gpt56-eval.py": re.compile(r"MATRIX = \{(.*?)\n\}", re.S),
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


def _check_role_projection(root: Path, errors: list[str]) -> None:
    canonical_path = root / "templates/model-requests.json"
    config_path = root / "templates/gsd-config.base.json"
    if not canonical_path.is_file() or not config_path.is_file():
        return
    canonical = json.loads(canonical_path.read_text())
    overrides = json.loads(config_path.read_text()).get("model_overrides", {})
    for role, request in canonical.items():
        if role in ROLE_ALLOWLIST:
            continue
        tier = request.get("name") if isinstance(request, dict) else None
        expected = TIER_ALIAS.get(tier)
        if expected is None:
            errors.append(f"{canonical_path}: role {role!r} has unknown tier {tier!r}")
            continue
        if role not in overrides:
            errors.append(f"{config_path}: role {role!r} missing from model_overrides (F8)")
            continue
        actual = overrides[role]
        if actual != expected:
            errors.append(
                f"{config_path}: role {role!r} pinned to {actual!r}, "
                f"expected {expected!r} for tier {tier!r}"
            )
    for role in overrides:
        if role not in canonical and role not in ROLE_ALLOWLIST:
            errors.append(f"{config_path}: role {role!r} not present in {canonical_path}")


def _check_tier_models_shape(root: Path, errors: list[str]) -> None:
    config_path = root / "templates/gsd-config.base.json"
    if not config_path.is_file():
        return
    tier_models = json.loads(config_path.read_text()).get("dynamic_routing", {}).get(
        "tier_models", {}
    )
    if tier_models != TIER_MODELS_EXPECTED:
        errors.append(
            f"{config_path}: dynamic_routing.tier_models must be exactly "
            f"{TIER_MODELS_EXPECTED} (light/standard/heavy -> volume/execution/judgment), "
            f"got {tier_models}"
        )
    if "frontier" in tier_models or "fable" in tier_models.values():
        errors.append(
            f"{config_path}: frontier tier must never be reachable via "
            "dynamic_routing.tier_models (escalation must not auto-select the "
            "most expensive tier)"
        )


def _check_hardcoded_consumers(root: Path, errors: list[str]) -> None:
    for relpath, pattern in HARDCODED_CONSUMERS.items():
        path = root / relpath
        if not path.is_file():
            continue
        source = path.read_text()
        match = pattern.search(source)
        body = match.group(1) if match else ""
        missing = [tier for tier in CANONICAL_TIERS if f'"{tier}"' not in body]
        if missing:
            errors.append(f"{path}: hardcoded tier set missing {missing} (stale consumer, F9)")


def _check_unknown_aliases(root: Path, errors: list[str]) -> None:
    config_path = root / "templates/gsd-config.base.json"
    if not config_path.is_file():
        return
    config = json.loads(config_path.read_text())
    for role, alias in config.get("model_overrides", {}).items():
        if alias not in KNOWN_ALIASES:
            errors.append(f"{config_path}: unknown alias {alias!r} for role {role!r} (EDGE-004)")
    for rung, alias in config.get("dynamic_routing", {}).get("tier_models", {}).items():
        if alias not in KNOWN_ALIASES:
            errors.append(
                f"{config_path}: unknown alias {alias!r} for tier_models.{rung} (EDGE-004)"
            )


def lint(root: Path) -> list[str]:
    errors: list[str] = []
    _check_role_projection(root, errors)
    _check_tier_models_shape(root, errors)
    _check_hardcoded_consumers(root, errors)
    _check_unknown_aliases(root, errors)
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
