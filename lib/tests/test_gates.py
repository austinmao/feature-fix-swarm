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


# ── codex-gate remediation round (PR #13) ────────────────────────────────────

def test_run_gate_binds_evidence_to_real_execution(tmp_path) -> None:
    """P1: evidence must come from the runner, not caller-supplied --exit."""
    store = tmp_path / "evidence.json"
    rc = gates.run_gate(store, "T050", ["python3", "-c", "raise SystemExit(0)"])
    assert rc == 0 and gates.verify_done(store, "T050") is True
    rc = gates.run_gate(store, "T051", ["python3", "-c", "raise SystemExit(3)"])
    assert rc == 3 and gates.verify_done(store, "T051") is False


def test_run_red_uses_real_exit_code(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    # passing command can never be a RED proof
    assert gates.run_red(store, "T052", ["python3", "-c", "print('ok')"]) is False
    assert gates.check_red(store, "T052") is False
    # genuinely failing command is
    assert gates.run_red(store, "T052", ["python3", "-c",
                         "print('FAIL: boom'); raise SystemExit(1)"]) is True
    assert gates.check_red(store, "T052") is True


def test_red_markers_accept_bare_fail() -> None:
    """P1: go test prints bare FAIL — must count as a failure marker."""
    assert gates.FAILURE_MARKERS.search("FAIL\nexit status 1") is not None


def test_store_write_is_atomic_no_partial_file(tmp_path) -> None:
    """P1: writes go through tmp+rename — store is never truncated in place."""
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T060", exit_code=0, cmd="x")
    data = json.loads(store.read_text())
    assert data["T060"]["gate"]["exit_code"] == 0
    # no stray temp files left behind (the .lock file is an intentional artifact)
    leftovers = [p for p in store.parent.iterdir()
                 if p.name != "evidence.json" and p.suffix != ".lock"]
    assert leftovers == []


def test_scan_tamper_flags_weakened_assertions() -> None:
    """P2: assert True / expect(true) neutering must be flagged."""
    diff = (
        "--- a/lib/tests/test_x.py\n"
        "+++ b/lib/tests/test_x.py\n"
        "+    assert True\n"
    )
    findings = gates.scan_test_tampering(diff)
    assert any("always-true" in f or "weakened" in f for f in findings)


# ── codex-gate round 2 (PR #13) ──────────────────────────────────────────────

def test_analyze_detects_story_phase_from_task_tags_not_header() -> None:
    """P1: documented header format is '## Phase 3: User Story 1 — ...' (no USn
    token) — story detection must come from the [USn] tags on the phase's tasks."""
    spec = "# Spec\n## US1 — login works\n"
    tasks = (
        "# Tasks\n"
        "## Phase 3: User Story 1 — Login (Priority: P1) 🎯 MVP\n"
        "- [ ] T001 [US1] [model:sonnet] [agent:ecc:code-reviewer] /review-gate — review Phase 3 [qa:review-gate]\n"
    )
    findings = gates.analyze_artifacts(spec, tasks)
    assert any("e2e" in f for f in findings), findings


def test_scan_tamper_flags_deleted_asserts_in_source_files() -> None:
    """P1: deleting runtime asserts from impl files is a reward-hack too."""
    diff = (
        "--- a/lib/foo.py\n"
        "+++ b/lib/foo.py\n"
        "-    assert balance >= 0, 'invariant'\n"
        "+    pass\n"
    )
    findings = gates.scan_test_tampering(diff)
    assert any("assert" in f for f in findings), findings


# ── v3.14: strict evidence provenance ────────────────────────────────────────

def test_verify_done_strict_rejects_caller_recorded(tmp_path) -> None:
    """record-gate evidence is trusted-caller; strict mode must reject it."""
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T020", exit_code=0, cmd="pytest -q")
    assert gates.verify_done(store, "T020") is True          # lax: unchanged
    assert gates.verify_done(store, "T020", strict=True) is False


def test_verify_done_strict_accepts_runner_evidence(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    rc = gates.run_gate(store, "T021", ["python3", "-c", "print('ok')"])
    assert rc == 0
    assert gates.verify_done(store, "T021", strict=True) is True


def test_record_gate_stamps_executed_by_caller(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T022", exit_code=0, cmd="pytest -q")
    data = json.loads(store.read_text())
    assert data["T022"]["gate"]["executed_by"] == "caller"


def test_cli_record_gate_warns_and_strict_env_rejects(tmp_path) -> None:
    """CLI: record-gate prints a runtime WARNING; GATES_STRICT=1 makes
    verify-done reject caller-recorded evidence."""
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    r = _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "record-gate", "T023",
                 "--exit", "0", "--cmd", "pytest -q"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "WARNING" in r.stderr and "run-gate" in r.stderr
    # lax verify passes
    r2 = _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "verify-done", "T023"],
                 capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "executed_by=caller" in r2.stdout
    # strict verify rejects
    env_strict = dict(env, GATES_STRICT="1")
    r3 = _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "verify-done", "T023"],
                 capture_output=True, text=True, env=env_strict)
    assert r3.returncode == 1 and "runner" in r3.stdout


# ── v3.14: phase truth score (wires previously-dead truth_score) ─────────────

def test_classify_gate_cmd() -> None:
    assert gates.classify_gate_cmd("pytest -q") == "tests"
    assert gates.classify_gate_cmd("npx tsc --noEmit") == "typecheck"
    assert gates.classify_gate_cmd("ruff check .") == "lint"
    assert gates.classify_gate_cmd("go build ./...") == "compile"
    assert gates.classify_gate_cmd("something-else") == "tests"  # default


def test_phase_score_all_green_is_one(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T030", exit_code=0, cmd="pytest -q")
    gates.record_gate_evidence(store, "T031", exit_code=0, cmd="ruff check .")
    score, breakdown = gates.phase_score(store, ["T030", "T031"])
    assert score == 1.0
    assert breakdown == {"tests": True, "lint": True}


def test_phase_score_failed_category_drops_score(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T032", exit_code=0, cmd="pytest -q")
    gates.record_gate_evidence(store, "T033", exit_code=1, cmd="ruff check .")
    score, breakdown = gates.phase_score(store, ["T032", "T033"])
    assert breakdown == {"tests": True, "lint": False}
    # normalized: tests .25 / (.25 + .20) ≈ 0.56 — well under threshold
    assert score < gates.TRUTH_THRESHOLD


def test_phase_score_missing_evidence_is_zero(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T034", exit_code=0, cmd="pytest -q")
    score, breakdown = gates.phase_score(store, ["T034", "T035"])  # T035 has none
    assert score == 0.0
    assert "T035" in breakdown.get("missing", [])


def test_cli_phase_score_exit_codes(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "record-gate", "T036",
             "--exit", "0", "--cmd", "pytest -q"], capture_output=True, env=env)
    r = _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "phase-score", "T036"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "TRUTH-SCORE 1.00" in r.stdout
    _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "record-gate", "T037",
             "--exit", "1", "--cmd", "npx tsc --noEmit"], capture_output=True, env=env)
    r2 = _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "phase-score",
                  "T036", "T037"], capture_output=True, text=True, env=env)
    assert r2.returncode == 1  # below 0.95 threshold → rollback signal


# ── v3.14: no-progress wired into the store (note-failure) ───────────────────

def test_note_failure_detects_stuck_loop(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.note_failure(store, "T040", "AssertionError foo.py:12") is False
    assert gates.note_failure(store, "T040", "AssertionError foo.py:12") is True
    # a DIFFERENT signature is progress again
    assert gates.note_failure(store, "T040", "TypeError bar.py:9") is False


def test_cli_note_failure_exit_codes(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    a = ["python3", str(DISPATCH_DIR / "gates.py"), "note-failure", "T041",
         "--sig", "AssertionError foo.py:12"]
    r1 = _sp.run(a, capture_output=True, text=True, env=env)
    assert r1.returncode == 0 and "PROGRESS-OK" in r1.stdout
    r2 = _sp.run(a, capture_output=True, text=True, env=env)
    assert r2.returncode == 1 and "NO-PROGRESS" in r2.stdout
