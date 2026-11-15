"""Static wiring checks for the F1 intent contract."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "<!-- ffs:intent -->"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


def test_scope_drift_gate_script_is_deleted() -> None:
    assert not (ROOT / "scripts/gsd/scope-drift-gate.sh").exists()


def test_no_scope_drift_gate_references_in_scripts_or_skills() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in [*(ROOT / "scripts/gsd").glob("*.sh"), *(ROOT / "skills").glob("*/SKILL.md")]
        if "scope-drift-gate" in p.read_text()
    ]
    assert offenders == []


def test_drift_model_request_removed() -> None:
    for rel in ("scripts/lint_model_routing.py", "docs/configuration.md"):
        assert "GSD_DRIFT_MODEL_REQUEST" not in _read(rel), rel


def test_review_gate_skill_runs_intent_check_before_meter() -> None:
    text = _read("skills/review-gate/SKILL.md")
    assert "intent-check" in text
    assert "--stage finish" in text
    assert "review:spec --max 2" in text
    assert text.index("intent-check") < text.index("review:spec --max 2")


def test_spec_templates_write_intent_block() -> None:
    for rel in ("skills/feature-spec/SKILL.md", "skills/plan-decompose/SKILL.md"):
        text = _read(rel)
        assert MARKER in text, rel
        assert "prod_delta:" in text, rel


def test_review_gate_command_runs_intent_check_before_loop_round() -> None:
    text = _read("scripts/gsd/review-gate-command.sh")
    assert "intent-check" in text
    assert "loop-round" in text
    assert text.index("intent-check") < text.index("loop-round")
