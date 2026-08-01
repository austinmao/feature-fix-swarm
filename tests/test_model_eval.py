from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.model_eval import EvalResultError, choose_lower_effort, validate_result


ROOT = Path(__file__).resolve().parents[1]


def _result(repetition: int, *, effort: str = "medium", passed: bool = True) -> dict:
    return {
        "fixture_id": "execution-medium-1",
        "repetition": repetition,
        "tier": "execution",
        "blast_radius": "medium",
        "corpus_sha256": "a" * 64,
        "codex_cli_version": "0.146.0",
        "model_id": "gpt-5.6-terra",
        "effort": effort,
        "date": "2026-08-01T00:00:00Z",
        "latency_ms": 1200,
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "retries": 0,
        "mandatory_gates_passed": passed,
        "findings": [],
        "requirement_coverage_regression": False,
    }


def test_corpus_has_six_cases_per_tier_and_all_blast_radii() -> None:
    cases = json.loads((ROOT / "evals/gpt56/corpus.json").read_text())
    assert len(cases) == 18
    for tier in ("judgment", "execution", "volume"):
        tier_cases = [case for case in cases if case["tier"] == tier]
        assert len(tier_cases) == 6
        assert {case["blast_radius"] for case in tier_cases} == {"low", "medium", "high"}


def test_lower_effort_requires_two_clean_repetitions() -> None:
    results = [_result(1), _result(2)]
    assert choose_lower_effort(results, higher="high", lower="medium") == "medium"


@pytest.mark.parametrize(
    "mutation",
    [
        {"mandatory_gates_passed": False},
        {"findings": [{"severity": "HIGH", "message": "regression"}]},
        {"requirement_coverage_regression": True},
    ],
)
def test_lower_effort_is_rejected_on_any_mandatory_regression(mutation: dict) -> None:
    first, second = _result(1), _result(2)
    second.update(mutation)
    assert choose_lower_effort([first, second], higher="high", lower="medium") == "high"


def test_result_schema_requires_reproducibility_metadata() -> None:
    result = _result(1)
    validate_result(result)
    del result["codex_cli_version"]
    with pytest.raises(EvalResultError, match="codex_cli_version"):
        validate_result(result)

