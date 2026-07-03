"""Tests for lib/gates.py — machine completion authority, RED proof,
truth score, tamper scan, no-progress detection, spec/tasks analyze.

Streams A-G of the v4 human-out-of-loop hardening plan.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

DISPATCH_DIR = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, DISPATCH_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


gates = _load("gates")


# ── Stream A: completion authority ───────────────────────────────────────────

def test_verify_done_requires_recorded_evidence(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.verify_done(store, "T001") is False  # no store at all
    gates.record_gate_evidence(store, "T001", exit_code=0, cmd="pytest -q",
                               tests_before="6 passed", tests_after="8 passed")
    assert gates.verify_done(store, "T001") is True
    assert gates.verify_done(store, "T999") is False  # other tasks unaffected


def test_verify_done_rejects_nonzero_exit(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T002", exit_code=1, cmd="pytest -q",
                               tests_before="8 passed", tests_after="1 failed")
    assert gates.verify_done(store, "T002") is False


def test_results_log_is_append_only(tmp_path) -> None:
    log = tmp_path / "results.md"
    gates.append_result(log, "T001 gate PASS 8/8")
    gates.append_result(log, "T002 gate FAIL 7/8")
    lines = log.read_text().splitlines()
    assert lines[0].endswith("T001 gate PASS 8/8")
    assert lines[1].endswith("T002 gate FAIL 7/8")


# ── Stream B: RED proof ──────────────────────────────────────────────────────

def test_red_proof_requires_actual_failure_output(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    # a "failing test" log that shows everything passing is NOT a RED proof
    ok = gates.record_red_proof(store, "T010", "8 passed in 0.01s", exit_code=0)
    assert ok is False
    assert gates.check_red(store, "T010") is False
    ok = gates.record_red_proof(store, "T010", "2 failed, 6 passed", exit_code=1)
    assert ok is True
    assert gates.check_red(store, "T010") is True


# ── Stream C: truth score + ladder ───────────────────────────────────────────

def test_truth_score_weights_match_spec() -> None:
    assert gates.truth_score(True, True, True, True) == 1.0
    assert gates.truth_score(False, True, True, True) == 0.65   # compile 0.35
    assert gates.truth_score(True, False, True, True) == 0.75   # tests   0.25
    assert gates.truth_score(True, True, False, True) == 0.80   # lint    0.20
    assert gates.truth_score(True, True, True, False) == 0.80   # typechk 0.20


def test_gate_ladder_orders_cheap_before_expensive() -> None:
    ladder = gates.GATE_LADDER
    assert ladder.index("compile") < ladder.index("unit")
    assert ladder.index("unit") < ladder.index("e2e")
    assert ladder.index("e2e") < ladder.index("review")


# ── Stream D: reward-hacking guards ──────────────────────────────────────────

def test_scan_tamper_flags_deleted_asserts_and_skips() -> None:
    diff = (
        "--- a/lib/tests/test_x.py\n"
        "+++ b/lib/tests/test_x.py\n"
        "-    assert result == expected\n"
        "+    pass\n"
        "+    @pytest.mark.skip\n"
    )
    findings = gates.scan_test_tampering(diff)
    joined = " ".join(findings).lower()
    assert "assert" in joined
    assert "skip" in joined


def test_scan_tamper_flags_exit_zero_and_ci_edits() -> None:
    diff = (
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "+      run: exit 0\n"
    )
    findings = gates.scan_test_tampering(diff)
    joined = " ".join(findings).lower()
    assert "ci" in joined or "workflow" in joined
    assert "exit 0" in joined


def test_scan_tamper_clean_diff_returns_empty() -> None:
    diff = (
        "--- a/lib/gates.py\n"
        "+++ b/lib/gates.py\n"
        "+def helper():\n"
        "+    return 1\n"
    )
    assert gates.scan_test_tampering(diff) == []


def test_impl_task_may_not_touch_test_files() -> None:
    violations = gates.check_test_separation(
        ["lib/gates.py", "lib/tests/test_gates.py"], task_kind="impl")
    assert violations == ["lib/tests/test_gates.py"]
    # test-author tasks may touch tests freely
    assert gates.check_test_separation(
        ["lib/tests/test_gates.py"], task_kind="test") == []


# ── Stream E: no-progress detection ──────────────────────────────────────────

def test_no_progress_when_same_failure_repeats() -> None:
    assert gates.no_progress(["FAILED test_a - AssertionError",
                              "FAILED test_a - AssertionError"]) is True
    assert gates.no_progress(["FAILED test_a", "FAILED test_b"]) is False
    assert gates.no_progress(["FAILED test_a"]) is False


# ── Stream G: spec/tasks coherence analyze ───────────────────────────────────

SPEC = """# Spec
## US1 — login works
## US2 — logout works
"""

TASKS_OK = """# Tasks
## Phase 3 — US1
- [ ] T001 [US1] [model:sonnet] [qa:e2e] [agent:test-automator] e2e smoke login
- [ ] T002 [US1] [model:sonnet] [agent:ecc:code-reviewer] /review-gate — review Phase 3 [qa:review-gate]
## Phase 4 — US2
- [ ] T003 [US2] [model:sonnet] [qa:e2e] [agent:test-automator] e2e smoke logout
- [ ] T004 [US2] [model:sonnet] [agent:ecc:code-reviewer] /review-gate — review Phase 4 [qa:review-gate]
"""

TASKS_BAD = """# Tasks
## Phase 3 — US1
- [ ] T001 [US3] [model:sonnet] implement mystery story
"""


def test_analyze_passes_coherent_artifacts() -> None:
    assert gates.analyze_artifacts(SPEC, TASKS_OK) == []


def test_analyze_flags_orphan_story_missing_gate_and_e2e() -> None:
    findings = gates.analyze_artifacts(SPEC, TASKS_BAD)
    joined = " ".join(findings)
    assert "US3" in joined            # task references story not in spec
    assert "US1" in joined or "US2" in joined  # spec stories with no tasks
    assert "review-gate" in joined    # phase without gate task
    assert "e2e" in joined            # phase without e2e smoke task
