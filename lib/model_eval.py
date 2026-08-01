"""Deterministic acceptance rules for the FFS GPT-5.6 effort evaluation."""

from __future__ import annotations

from typing import Any, Iterable


class EvalResultError(ValueError):
    pass


REQUIRED = {
    "fixture_id",
    "repetition",
    "tier",
    "blast_radius",
    "corpus_sha256",
    "codex_cli_version",
    "model_id",
    "effort",
    "date",
    "latency_ms",
    "usage",
    "retries",
    "mandatory_gates_passed",
    "findings",
    "requirement_coverage_regression",
}


def validate_result(result: dict[str, Any]) -> None:
    missing = sorted(REQUIRED - set(result))
    if missing:
        raise EvalResultError(f"missing required result field: {', '.join(missing)}")
    if result["repetition"] not in {1, 2}:
        raise EvalResultError("repetition must be 1 or 2")
    if result["tier"] not in {"judgment", "execution", "volume"}:
        raise EvalResultError("invalid tier")
    if result["blast_radius"] not in {"low", "medium", "high"}:
        raise EvalResultError("invalid blast_radius")


def choose_lower_effort(
    results: Iterable[dict[str, Any]], *, higher: str, lower: str
) -> str:
    rows = list(results)
    if {row.get("repetition") for row in rows} != {1, 2} or len(rows) != 2:
        return higher
    for row in rows:
        validate_result(row)
        severe = any(
            str(item.get("severity", "")).upper() in {"BLOCKER", "HIGH"}
            for item in row["findings"]
        )
        if (
            row["effort"] != lower
            or not row["mandatory_gates_passed"]
            or severe
            or row["requirement_coverage_regression"]
        ):
            return higher
    return lower

