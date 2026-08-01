from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lint_model_routing", ROOT / "scripts/lint_model_routing.py"
)
assert SPEC and SPEC.loader
LINTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LINTER)


def test_repository_model_routing_surfaces_are_typed() -> None:
    assert LINTER.lint(ROOT) == []


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
