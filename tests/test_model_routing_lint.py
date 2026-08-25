from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lint_model_routing", ROOT / "scripts/lint_model_routing.py"
)
assert SPEC and SPEC.loader
LINTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LINTER)


CANONICAL = {
    "orchestrator": {"kind": "tier", "name": "execution"},
    "gsd-planner": {"kind": "tier", "name": "frontier"},
    "gsd-plan-checker": {"kind": "tier", "name": "judgment"},
    "gsd-executor": {"kind": "tier", "name": "execution"},
    "gsd-codebase-mapper": {"kind": "tier", "name": "execution"},
    "spec-status": {"kind": "tier", "name": "volume"},
}
AGREEING_OVERRIDES = {
    "gsd-planner": "fable",
    "gsd-plan-checker": "opus",
    "gsd-executor": "sonnet",
    "gsd-codebase-mapper": "sonnet",
}
AGREEING_TIER_MODELS = {"light": "haiku", "standard": "sonnet", "heavy": "opus"}
GSD_RUN_SNIPPET = (
    'tier = {"frontier": "fable", "judgment": "opus", "execution": "sonnet", '
    '"volume": "haiku"}.get(name, model)'
)
MODEL_EVAL_SNIPPET = 'result["tier"] not in {"frontier", "judgment", "execution", "volume"}'
MATRIX_SNIPPET = (
    "MATRIX = {\n"
    '    "frontier": ("gpt-5.6-sol", ("xhigh", "high")),\n'
    '    "judgment": ("gpt-5.6-sol", ("xhigh", "high")),\n'
    '    "execution": ("gpt-5.6-terra", ("high", "medium")),\n'
    '    "volume": ("gpt-5.6-luna", ("medium", "low")),\n'
    "}\n"
)


def _write_projection_fixture(
    tmp_path: Path,
    *,
    canonical: dict = CANONICAL,
    overrides: dict = AGREEING_OVERRIDES,
    tier_models: dict = AGREEING_TIER_MODELS,
    gsd_run_snippet: str = GSD_RUN_SNIPPET,
    model_eval_snippet: str = MODEL_EVAL_SNIPPET,
    matrix_snippet: str = MATRIX_SNIPPET,
) -> Path:
    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "templates/model-requests.json").write_text(json.dumps(canonical))
    (tmp_path / "templates/gsd-config.base.json").write_text(
        json.dumps(
            {
                "model_overrides": overrides,
                "dynamic_routing": {"tier_models": tier_models},
            }
        )
    )
    (tmp_path / "scripts/gsd").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts/gsd/gsd-run.sh").write_text(gsd_run_snippet)
    (tmp_path / "lib").mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib/model_eval.py").write_text(model_eval_snippet)
    (tmp_path / "scripts/run-gpt56-eval.py").write_text(matrix_snippet)
    return tmp_path


def test_repository_model_routing_surfaces_are_typed() -> None:
    assert LINTER.lint(ROOT) == []


def test_lint_catches_projection_drift(tmp_path: Path) -> None:
    overrides = dict(AGREEING_OVERRIDES)
    overrides["gsd-plan-checker"] = "sonnet"  # drift: judgment role pinned to execution alias
    fixture = _write_projection_fixture(tmp_path, overrides=overrides)
    errors = LINTER.lint(fixture)
    assert any("gsd-plan-checker" in error and "pinned to" in error for error in errors)


def test_lint_catches_missing_nonallowlisted_role(tmp_path: Path) -> None:
    overrides = dict(AGREEING_OVERRIDES)
    del overrides["gsd-codebase-mapper"]
    fixture = _write_projection_fixture(tmp_path, overrides=overrides)
    errors = LINTER.lint(fixture)
    assert any(
        "gsd-codebase-mapper" in error and "missing from model_overrides" in error
        for error in errors
    )


def test_lint_catches_tier_models_wrong_shape(tmp_path: Path) -> None:
    fixture = _write_projection_fixture(
        tmp_path, tier_models={"light": "haiku", "standard": "sonnet", "heavy": "fable"}
    )
    errors = LINTER.lint(fixture)
    assert any("tier_models must be exactly" in error for error in errors)


def test_lint_catches_frontier_reachable_via_dynamic_escalation(tmp_path: Path) -> None:
    fixture = _write_projection_fixture(
        tmp_path,
        tier_models={
            "light": "haiku",
            "standard": "sonnet",
            "heavy": "opus",
            "frontier": "fable",
        },
    )
    errors = LINTER.lint(fixture)
    assert any("frontier tier must never be reachable" in error for error in errors)


def test_lint_catches_stale_hardcoded_consumers(tmp_path: Path) -> None:
    fixture = _write_projection_fixture(
        tmp_path,
        gsd_run_snippet='tier = {"judgment": "opus", "execution": "sonnet", "volume": "haiku"}.get(name, model)',
        model_eval_snippet='result["tier"] not in {"judgment", "execution", "volume"}',
        matrix_snippet=(
            "MATRIX = {\n"
            '    "judgment": ("gpt-5.6-sol", ("xhigh", "high")),\n'
            '    "execution": ("gpt-5.6-terra", ("high", "medium")),\n'
            '    "volume": ("gpt-5.6-luna", ("medium", "low")),\n'
            "}\n"
        ),
    )
    errors = LINTER.lint(fixture)
    assert any("gsd-run.sh" in error and "frontier" in error for error in errors)
    assert any("model_eval.py" in error and "frontier" in error for error in errors)
    assert any("run-gpt56-eval.py" in error and "frontier" in error for error in errors)


def test_lint_catches_unknown_alias(tmp_path: Path) -> None:
    overrides = dict(AGREEING_OVERRIDES)
    overrides["gsd-plan-checker"] = "premium"
    fixture = _write_projection_fixture(tmp_path, overrides=overrides)
    errors = LINTER.lint(fixture)
    assert any("unknown alias" in error and "EDGE-004" in error for error in errors)


def test_lint_full_agreeing_fixture_has_no_projection_errors(tmp_path: Path) -> None:
    fixture = _write_projection_fixture(tmp_path)
    errors = LINTER.lint(fixture)
    projection_errors = [
        error
        for error in errors
        if "model_overrides" in error or "tier_models" in error or "hardcoded" in error
    ]
    assert projection_errors == []


def test_lint_rejects_raw_model_assignment_in_skill(tmp_path: Path) -> None:
    runtime = tmp_path / "scripts/gsd"
    runtime.mkdir(parents=True)
    for filename, variable in LINTER.RUNTIME_CONTRACTS.items():
        (runtime / filename).write_text(
            f'{variable}=typed\nadversary_invoke_typed_request "$kind" "$fallback"\n'
        )
    skill = tmp_path / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text('REVIEW_MODEL="gpt-5.6-sol"\n')

    assert any("raw vendor model ID" in error for error in LINTER.lint(tmp_path))


def test_lint_rejects_legacy_raw_runtime_expansion(tmp_path: Path) -> None:
    runtime = tmp_path / "scripts/gsd"
    runtime.mkdir(parents=True)
    for filename, variable in LINTER.RUNTIME_CONTRACTS.items():
        source = f'{variable}=typed\nadversary_invoke_typed_request "$kind" "$fallback"\n'
        if filename == "plan-adversary.sh":
            source += 'MODEL="${PLAN_ADVERSARY_MODEL:-gpt-5.6-sol}"\n'
        (runtime / filename).write_text(source)
    (tmp_path / "skills").mkdir()

    assert any("retired raw model variable" in error for error in LINTER.lint(tmp_path))
