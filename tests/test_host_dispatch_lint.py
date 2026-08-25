from __future__ import annotations

from pathlib import Path

from scripts.lint_host_dispatch import lint_skill_text


ROOT = Path(__file__).resolve().parents[1]


def test_annotated_cross_host_examples_are_allowed() -> None:
    text = """
Host dispatch:
- Codex: invoke `$gsd-plan-phase 2`.
- Claude: invoke `/gsd-plan-phase 2`.
"""
    assert lint_skill_text(text) == []


def test_unqualified_host_specific_calls_are_rejected() -> None:
    failures = lint_skill_text("Next run /gsd-plan-phase 2 with the Skill tool.")
    assert any("unqualified Claude skill call" in item for item in failures)
    assert any("unqualified host tool" in item for item in failures)


def test_unqualified_claude_model_alias_is_rejected() -> None:
    failures = lint_skill_text("Spawn an opus reviewer for this task.")
    assert any("unqualified Claude model alias" in item for item in failures)
    assert lint_skill_text("Claude: spawn an Opus reviewer for this task.") == []


def test_priority_skills_declare_the_shared_dispatch_contract() -> None:
    for name in (
        "feature-spec",
        "feature-implement",
        "spec-status",
        "spec-guide",
        "continue-compact",
    ):
        text = (ROOT / "skills" / name / "SKILL.md").read_text()
        assert "## Host dispatch contract" in text, name


def test_feature_implement_declares_the_wall_await_rule() -> None:
    text = (ROOT / "skills" / "feature-implement" / "SKILL.md").read_text()
    assert "## Wall await rule" in text


def test_entrypoint_skills_advertise_the_fail_soft_reconcile_pass() -> None:
    for name in ("feature-spec", "feature-implement", "task-swarm", "fix", "swarm", "code-uplift", "preflight"):
        text = (ROOT / "skills" / name / "SKILL.md").read_text()
        assert "bash scripts/gsd/reconcile.sh" in text, name
        assert text.index("bash scripts/gsd/reconcile.sh") > text.index("## Host dispatch contract"), name


def test_spec_decompose_declares_the_plan_length_gate_step() -> None:
    text = (ROOT / "skills" / "spec-decompose" / "SKILL.md").read_text()
    assert "### Step 3.5: Plan-length gate" in text


def test_entrypoint_skills_declare_the_init_gate() -> None:
    for name in (
        "feature-implement",
        "feature-spec",
        "task-swarm",
        "fix",
        "swarm",
        "code-uplift",
        "preflight",
    ):
        text = (ROOT / "skills" / name / "SKILL.md").read_text()
        assert "## Init gate" in text, name
        assert "init-guard.sh" in text, name


def test_continue_compact_keeps_compact_builtin_and_dispatches_resume() -> None:
    text = (ROOT / "skills" / "continue-compact" / "SKILL.md").read_text()
    assert "Do not redefine" in text
    assert "Codex: `$feature-implement" in text
    assert "Claude: `/feature-implement" in text


def test_every_shipped_skill_passes_host_dispatch_lint() -> None:
    failures: list[str] = []
    for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
        failures.extend(f"{path.parent.name}: {item}" for item in lint_skill_text(path.read_text()))
    assert failures == []


def test_feature_implement_declares_autonomous_rc3_auto_continue() -> None:
    text = (ROOT / "skills" / "feature-implement" / "SKILL.md").read_text()
    assert "### Autonomous rc-3 bounded auto-continue" in text
    assert "wall-autoreset:" in text
    assert "PLAN_WALL_AUTO_RESET_MAX" in text


def test_autonomy_grant_declares_wall_reset_type() -> None:
    text = (ROOT / "skills" / "autonomy-grant" / "SKILL.md").read_text()
    assert "`wall-reset`" in text


def test_feature_spec_max_auth_enumerates_wall_reset_per_phase() -> None:
    text = (ROOT / "skills" / "feature-spec" / "SKILL.md").read_text()
    assert "wall-reset:<phase-slug>" in text


def test_fix_round_mutation_contract_present_in_wall_skills() -> None:
    for name in ("feature-implement", "plan-decompose"):
        text = (ROOT / "skills" / name / "SKILL.md").read_text()
        assert "mutation contract" in text.lower(), name


def test_configuration_docs_cover_plan_wall_knobs() -> None:
    text = (ROOT / "docs" / "configuration.md").read_text()
    for var in ("PLAN_WALL_AUTO_RESET_MAX", "PLAN_WALL_MAX_ROUNDS",
                "PLAN_WALL_TIMEOUT", "PLAN_WALL_AWAIT_MAX"):
        assert var in text, var


def test_gsd_run_wraps_wall_calls_in_auto_continue_gate() -> None:
    text = (ROOT / "scripts" / "gsd" / "gsd-run.sh").read_text()
    assert "_gsd_run_wall_gate" in text
    assert "wall-autoreset:" in text
    # both wall call sites must route through the gate, none may bypass it
    assert 'bash "$PLAN_WALL_LEVER"' not in text.replace(
        '  bash "$PLAN_WALL_LEVER" "$phase_dir"', "")
