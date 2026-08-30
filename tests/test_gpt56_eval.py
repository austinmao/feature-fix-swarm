from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
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


def test_run_one_flags_missing_required_concept(tmp_path: Path) -> None:
    answer_text = "This response never states the required phrase."
    events = [
        _agent_message_event(json.dumps({"answer": answer_text})),
        _turn_completed_event({}),
    ]
    stub = _write_cli_stub(tmp_path, events)

    row = _run_with_stub(stub, tmp_path, must_include=["edge case"])

    assert {"severity": "HIGH", "message": "missing required concept: edge case"} in row["findings"]
    assert row["mandatory_gates_passed"] is False
    assert row["requirement_coverage_regression"] is True


def test_run_one_flags_non_json_event_line(tmp_path: Path) -> None:
    stub = _write_cli_stub(tmp_path, ["this stdout line is not JSON at all"])

    row = _run_with_stub(stub, tmp_path, must_include=["edge case"])

    assert {"severity": "HIGH", "message": "missing required concept: edge case"} in row["findings"]
    assert {"severity": "HIGH", "message": "non-JSON CLI event"} in row["findings"]
    assert (
        {"severity": "HIGH", "message": "final answer did not match the JSON schema"}
        in row["findings"]
    )
    assert row["mandatory_gates_passed"] is False
    assert row["requirement_coverage_regression"] is True


def test_run_one_flags_answer_that_is_not_schema_shaped(tmp_path: Path) -> None:
    # Valid JSON, but the top-level value is an array — not an object with
    # an "answer" field, so the schema lookup itself fails.
    events = [
        _agent_message_event(json.dumps(["not", "an", "object"])),
        _turn_completed_event({}),
    ]
    stub = _write_cli_stub(tmp_path, events)

    row = _run_with_stub(stub, tmp_path, must_include=["edge case"])

    assert (
        {"severity": "HIGH", "message": "final answer did not match the JSON schema"}
        in row["findings"]
    )
    assert row["mandatory_gates_passed"] is False


def test_run_one_stays_dirty_on_nonzero_exit_despite_clean_answer(tmp_path: Path) -> None:
    answer_text = "This clean answer covers the edge case fully."
    events = [
        _agent_message_event(json.dumps({"answer": answer_text})),
        _turn_completed_event({}),
    ]
    stub = _write_cli_stub(tmp_path, events, exit_code=3)

    row = _run_with_stub(stub, tmp_path, must_include=["edge case"])

    assert {"severity": "HIGH", "message": "Codex CLI exited 3"} in row["findings"]
    assert row["mandatory_gates_passed"] is False
    assert row["requirement_coverage_regression"] is False


def test_run_one_stays_dirty_on_timeout_despite_clean_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_text = "This clean partial answer covers the edge case fully."
    partial_stdout = (
        _agent_message_event(json.dumps({"answer": answer_text})) + "\n"
    )

    def _raise_timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=300, output=partial_stdout)

    monkeypatch.setattr(RUNNER.subprocess, "run", _raise_timeout)

    row = _run_with_stub(
        str(tmp_path / "unused-cli-stub"), tmp_path, must_include=["edge case"]
    )

    assert {"severity": "HIGH", "message": "Codex CLI exited 124"} in row["findings"]
    assert row["mandatory_gates_passed"] is False


def _corpus_matrix_rows() -> list[dict[str, object]]:
    """Build the row shape production's main() actually emits: for each
    real corpus fixture, each effort in its tier's matrix entry, times
    repetitions 1 and 2 — no hand-picked synthetic tier/fixture counts."""
    corpus = json.loads((ROOT / "evals/gpt56/corpus.json").read_text())
    rows: list[dict[str, object]] = []
    for fixture in corpus:
        model, efforts = RUNNER.MATRIX[fixture["tier"]]
        for effort in efforts:
            for repetition in (1, 2):
                rows.append(
                    {
                        "fixture_id": fixture["id"],
                        "repetition": repetition,
                        "tier": fixture["tier"],
                        "model_id": model,
                        "effort": effort,
                        "mandatory_gates_passed": True,
                        "findings": [],
                        "requirement_coverage_regression": False,
                    }
                )
    return rows


def test_corpus_matrix_builder_yields_72_rows_12_per_effort() -> None:
    rows = _corpus_matrix_rows()

    assert len(rows) == 72
    for tier, (_model, efforts) in RUNNER.MATRIX.items():
        if len(efforts) < 2:
            continue
        for effort in efforts:
            count = sum(1 for row in rows if row["tier"] == tier and row["effort"] == effort)
            assert count == 12


def test_selections_choose_lower_effort_for_real_corpus_matrix() -> None:
    rows = _corpus_matrix_rows()

    decision = RUNNER.selections(rows)

    assert decision == {
        tier: efforts[1]
        for tier, (_model, efforts) in RUNNER.MATRIX.items()
        if len(efforts) >= 2
    }
    assert "frontier" not in decision


@pytest.mark.parametrize(
    "mutation",
    [
        {"findings": [{"severity": "HIGH", "message": "regression"}]},
        {"requirement_coverage_regression": True},
    ],
)
def test_selections_fall_back_to_higher_effort_on_dirty_lower_row_real_corpus(
    mutation: dict[str, object],
) -> None:
    rows = _corpus_matrix_rows()
    target = next(
        row
        for row in rows
        if row["tier"] == "execution" and row["effort"] == RUNNER.MATRIX["execution"][1][1]
    )
    target.update(mutation)

    decision = RUNNER.selections(rows)

    assert decision["execution"] == RUNNER.MATRIX["execution"][1][0]
    for tier, (_model, efforts) in RUNNER.MATRIX.items():
        if tier == "execution" or len(efforts) < 2:
            continue
        assert decision[tier] == efforts[1]


def test_selections_fail_closed_on_incomplete_real_corpus_matrix() -> None:
    rows = _corpus_matrix_rows()
    rows.pop()

    with pytest.raises(SystemExit, match="incomplete volume evaluation matrix"):
        RUNNER.selections(rows)


def test_selections_fail_closed_on_dirty_higher_effort_baseline_real_corpus() -> None:
    rows = _corpus_matrix_rows()
    target = next(
        row
        for row in rows
        if row["tier"] == "execution" and row["effort"] == RUNNER.MATRIX["execution"][1][0]
    )
    target["findings"] = [{"severity": "HIGH", "message": "regression"}]

    with pytest.raises(SystemExit, match="higher-effort execution baseline failed"):
        RUNNER.selections(rows)
