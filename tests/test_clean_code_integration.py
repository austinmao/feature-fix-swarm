"""Clean-code rules reach the code-writing agent before it writes.

Text pins only prove the rules are delivered; the behavioral proof is the
fixture A/B run recorded in the change's PR."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


def test_executor_loads_testing_policy_through_agent_skills() -> None:
    config = json.loads(_read("templates/gsd-config.base.json"))
    skills = config["agent_skills"]["gsd-executor"]
    assert "global:testing-policy" in skills  # user-scope install
    assert ".agents/skills/testing-policy" in skills  # project-scope install (both hosts link it)


def test_testing_policy_carries_write_time_rules() -> None:
    text = _read("skills/testing-policy/SKILL.md")
    for rule in (
        "One decision lives in one place",
        "Subtract before you add",
        "Check data where it enters",
        "a guard that only hides the crash is not a fix",
        "cyclomatic complexity under 10",
    ):
        assert rule in text, rule
    assert "would still pass if the code under test returned nothing" in text
    assert "Tests asserting mock-call counts instead of observable behavior." not in text


def test_adhoc_task_string_carries_the_rules() -> None:
    text = _read("skills/feature-implement/SKILL.md")
    assert "testing-policy §7" in text
    assert "TDD applies: failing repro/behavior test first (RED), then the\nchange (GREEN)" not in text


def test_plan_wall_flags_bolt_on_steps() -> None:
    clause = "and steps that add a new path beside an old one without naming the old path it retires"
    assert clause in _read("scripts/gsd/plan-wall.sh")
    assert clause in _read("tests/bats/socratic-plan-wall-baseline.prompt")
