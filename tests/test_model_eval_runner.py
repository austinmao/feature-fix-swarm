from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_gpt56_eval", ROOT / "scripts/run-gpt56-eval.py"
)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def _matrix_results() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for tier, (model, efforts) in RUNNER.MATRIX.items():
        for effort in efforts:
            for fixture in range(6):
                for repetition in (1, 2):
                    rows.append(
                        {
                            "fixture_id": f"{tier}-{fixture}",
                            "repetition": repetition,
                            "tier": tier,
                            "model_id": model,
                            "effort": effort,
                            "mandatory_gates_passed": True,
                            "findings": [],
                            "requirement_coverage_regression": False,
                        }
                    )
    return rows


def test_eval_refuses_custom_provider_before_codex_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid")
    with pytest.raises(SystemExit, match="custom model providers are unsupported"):
        RUNNER.run_one(
            binary="must-not-run",
            codex_home=tmp_path,
            schema_path=tmp_path / "schema.json",
            fixture={
                "id": "volume-low-1",
                "tier": "volume",
                "blast_radius": "low",
                "task": "bounded",
                "must_include": ["bounded"],
            },
            model="gpt-5.6-luna",
            effort="low",
            repetition=1,
            corpus_hash="a" * 64,
            cli_version="0.146.0",
            cwd=tmp_path,
        )


def test_eval_auth_cas_uses_runner_compatible_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = home / ".codex/auth.json"
    source.parent.mkdir()
    source.write_text('{"refresh":"initial"}\n')
    os.chmod(source, 0o600)
    refreshed = tmp_path / "refreshed.json"
    refreshed.write_text('{"refresh":"rotated"}\n')
    os.chmod(refreshed, 0o600)

    RUNNER.sync_refreshed_auth(source, refreshed, RUNNER.sha256(source))

    assert source.read_text() == refreshed.read_text()
    lock = home / ".cache/feature-fix-swarm/codex-auth.lock"
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600


def test_eval_synchronizes_auth_when_evaluation_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = ROOT / "evals/gpt56/corpus.json"
    auth = tmp_path / "auth.json"
    auth.write_text('{"refresh":"initial"}\n')
    os.chmod(auth, 0o600)
    output = tmp_path / "results.json"
    synchronized: list[Path] = []

    monkeypatch.setattr(RUNNER, "codex_version", lambda _binary: "0.146.0")
    monkeypatch.setattr(
        RUNNER,
        "run_one",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        RUNNER,
        "sync_refreshed_auth",
        lambda _source, refreshed, _initial: synchronized.append(refreshed),
    )

    with pytest.raises(KeyboardInterrupt):
        RUNNER.main(
            [
                "--corpus",
                str(corpus),
                "--output",
                str(output),
                "--codex-bin",
                "unused",
                "--auth",
                str(auth),
            ]
        )

    assert len(synchronized) == 1


def test_selections_choose_lower_effort_for_complete_clean_matrix() -> None:
    # "frontier" is a single-effort pinned reference row (spec-004 fix round
    # finding 15) — selections() has nothing to choose between for it, so it
    # is excluded from the expected decision dict, same as selections()
    # itself skips it.
    assert RUNNER.selections(_matrix_results()) == {
        tier: efforts[1]
        for tier, (_model, efforts) in RUNNER.MATRIX.items()
        if len(efforts) >= 2
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"findings": [{"severity": "CRITICAL", "message": "regression"}]},
        {"requirement_coverage_regression": True},
    ],
)
def test_selections_reject_dirty_lower_effort_rows(mutation: dict[str, object]) -> None:
    rows = _matrix_results()
    target = next(
        row
        for row in rows
        if row["tier"] == "execution"
        and row["effort"] == RUNNER.MATRIX["execution"][1][1]
    )
    target.update(mutation)

    selected = RUNNER.selections(rows)

    assert selected["execution"] == RUNNER.MATRIX["execution"][1][0]


def _first_row_for_a_two_arm_tier(rows: list[dict[str, object]]) -> dict[str, object]:
    """First row belonging to a tier selections() actually validates —
    "frontier" (spec-004 fix round finding 15: single-effort pinned
    reference) is skipped by selections() entirely, so mutating a frontier
    row would never surface as a SystemExit."""
    return next(
        row for row in rows
        if len(RUNNER.MATRIX[row["tier"]][1]) >= 2
    )


def test_selections_fail_closed_on_non_boolean_gate_result() -> None:
    rows = _matrix_results()
    _first_row_for_a_two_arm_tier(rows)["mandatory_gates_passed"] = "false"

    with pytest.raises(SystemExit, match="mandatory_gates_passed must be boolean"):
        RUNNER.selections(rows)


@pytest.mark.parametrize(
    "finding",
    [
        {"severity": "CRITICAL ", "message": "malformed"},
        {"message": "missing severity"},
    ],
)
def test_selections_fail_closed_on_unknown_finding_severity(
    finding: dict[str, str],
) -> None:
    rows = _matrix_results()
    rows[-1]["findings"] = [finding]

    with pytest.raises(SystemExit, match="finding severity is invalid"):
        RUNNER.selections(rows)


def test_selections_fail_closed_on_incomplete_matrix() -> None:
    rows = _matrix_results()
    rows.pop()

    with pytest.raises(SystemExit, match="incomplete volume evaluation matrix"):
        RUNNER.selections(rows)


def test_selections_fail_when_higher_effort_baseline_is_dirty() -> None:
    rows = _matrix_results()
    dirty_row = _first_row_for_a_two_arm_tier(rows)
    dirty_tier = dirty_row["tier"]
    dirty_row["findings"] = [{"severity": "HIGH", "message": "regression"}]

    # regex is derived from the dirtied row's own tier, not hardcoded to
    # "judgment" — spec-004 AC-003 added a `frontier` MATRIX row ahead of
    # judgment, and the fix round (finding 15) made frontier single-effort
    # and therefore skipped entirely by selections(), so the dirtied row
    # must land on the first tier selections() actually validates rather
    # than blindly rows[0] (pre-existing test staleness pinned to
    # "judgment" pre-Phase-2, unrelated to Phase 2 itself).
    with pytest.raises(SystemExit, match=f"higher-effort {dirty_tier} baseline failed"):
        RUNNER.selections(rows)
