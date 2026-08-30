from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_gpt56_eval", ROOT / "scripts/run-gpt56-eval.py"
)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def _write_cli_stub(tmp_path: Path, lines: list[str], *, exit_code: int = 0) -> Path:
    """Write an executable shell stub that emits fixed literal stdout lines
    and exits with the given status. Never interpolates an environment
    variable — the caller supplies every line verbatim."""
    stub = tmp_path / "cli-stub.sh"
    body = "\n".join(lines)
    stub.write_text(
        "#!/bin/sh\n"
        "cat <<'STUB_EVENT_STREAM'\n"
        f"{body}\n"
        "STUB_EVENT_STREAM\n"
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)
    return stub


def _agent_message_event(text: str) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


def _turn_completed_event(usage: dict[str, object]) -> str:
    return json.dumps({"type": "turn.completed", "usage": usage})


def _run_with_stub(
    binary: str | Path,
    tmp_path: Path,
    *,
    must_include: list[str] | None = None,
    task: str = "Review a one-line change for correctness.",
    fixture_id: str = "quick-35m-fixture-1",
    tier: str = "judgment",
    blast_radius: str = "low",
    model: str = "gpt-5.6-sol",
    effort: str = "xhigh",
    repetition: int = 1,
    corpus_hash: str = "b" * 64,
    cli_version: str = "0.150.0",
) -> dict[str, object]:
    return RUNNER.run_one(
        binary=str(binary),
        codex_home=tmp_path / "cli-home",
        schema_path=tmp_path / "response.schema.json",
        fixture={
            "id": fixture_id,
            "tier": tier,
            "blast_radius": blast_radius,
            "task": task,
            "must_include": must_include if must_include is not None else ["edge case"],
        },
        model=model,
        effort=effort,
        repetition=repetition,
        corpus_hash=corpus_hash,
        cli_version=cli_version,
        cwd=tmp_path,
    )


def test_run_one_produces_a_clean_row_end_to_end(tmp_path: Path) -> None:
    usage = {"input_tokens": 41, "output_tokens": 9}
    answer_text = "The change is correct; the edge case is an empty input."
    events = [
        _agent_message_event(json.dumps({"answer": answer_text})),
        _turn_completed_event(usage),
    ]
    stub = _write_cli_stub(tmp_path, events)

    row = _run_with_stub(
        stub,
        tmp_path,
        must_include=["edge case"],
        fixture_id="judgment-low-1",
        tier="judgment",
        blast_radius="low",
        model="gpt-5.6-sol",
        effort="xhigh",
        repetition=1,
        corpus_hash="c" * 64,
        cli_version="0.150.0",
    )

    assert row["usage"] == usage
    assert row["answer_sha256"] == hashlib.sha256(answer_text.encode()).hexdigest()
    assert row["findings"] == []
    assert row["mandatory_gates_passed"] is True
    assert row["requirement_coverage_regression"] is False
    assert isinstance(row["latency_ms"], int)
    assert row["latency_ms"] >= 0
    assert row["fixture_id"] == "judgment-low-1"
    assert row["tier"] == "judgment"
    assert row["blast_radius"] == "low"
    assert row["repetition"] == 1
    assert row["model_id"] == "gpt-5.6-sol"
    assert row["effort"] == "xhigh"
    assert row["corpus_sha256"] == "c" * 64
    assert row["codex_cli_version"] == "0.150.0"
    assert row["retries"] == 0
