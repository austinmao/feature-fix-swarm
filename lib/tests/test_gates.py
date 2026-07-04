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


def test_phase_score_strict_ignores_caller_evidence(tmp_path) -> None:
    """Codex P1 (v3.14 gate round 1): phase-score is the rollback authority —
    under strict it must not count caller-recorded (forgeable) evidence."""
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T050", exit_code=0, cmd="pytest -q")  # caller
    score_lax, _ = gates.phase_score(store, ["T050"])
    assert score_lax == 1.0
    score_strict, breakdown = gates.phase_score(store, ["T050"], strict=True)
    assert score_strict == 0.0
    assert "T050" in breakdown.get("missing", [])
    # runner evidence still counts under strict
    gates.run_gate(store, "T051", ["python3", "-c", "print('ok')"])
    score2, _ = gates.phase_score(store, ["T051"], strict=True)
    assert score2 == 1.0


def test_cli_phase_score_strict_env(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"), GATES_STRICT="1")
    _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "record-gate", "T052",
             "--exit", "0", "--cmd", "pytest -q"], capture_output=True, env=env)
    r = _sp.run(["python3", str(DISPATCH_DIR / "gates.py"), "phase-score", "T052"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1  # caller evidence = unproven under strict


def test_note_failure_ignores_blank_signatures(tmp_path) -> None:
    """Codex round 2 P2: a blank signature must not count — two blanks in a
    row would otherwise stop the loop with a false NO-PROGRESS."""
    store = tmp_path / "evidence.json"
    assert gates.note_failure(store, "T060", "") is False
    assert gates.note_failure(store, "T060", "") is False   # two identical blanks: NOT stuck
    assert gates.note_failure(store, "T060", "   ") is False # whitespace-only: same
    # real signatures still work after blanks
    assert gates.note_failure(store, "T060", "AssertionError x") is False
    assert gates.note_failure(store, "T060", "AssertionError x") is True


def test_run_gate_stores_failure_signature_lines(tmp_path) -> None:
    """Codex round 2 P2: the retry key must be the failing lines, not the
    final summary line (different failures share '1 failed in ...')."""
    store = tmp_path / "evidence.json"
    code = (
        "import sys\n"
        "print('collected 2 items')\n"
        "print('FAILED test_a.py::test_x - AssertionError: boom')\n"
        "print('1 failed, 1 passed in 0.01s')\n"
        "sys.exit(1)\n"
    )
    rc = gates.run_gate(store, "T061", ["python3", "-c", code])
    assert rc == 1
    sig = json.loads(store.read_text())["T061"]["gate"].get("failure_sig", "")
    assert "AssertionError: boom" in sig  # the discriminating line survives


def test_run_gate_failure_sig_survives_long_output(tmp_path) -> None:
    """Codex round 3 P2: markers must be scanned over the FULL output —
    truncating to the last 2000 chars first drops early traceback lines."""
    store = tmp_path / "evidence.json"
    code = (
        "import sys\n"
        "print('FAILED test_a.py::test_x - AssertionError: needle')\n"
        "print('filler line ' * 10 + '\\n' * 1 , end='')\n"
        "print(('x' * 80 + '\\n') * 40, end='')\n"   # >3000 chars of filler AFTER the marker
        "print('1 failed in 0.01s')\n"
        "sys.exit(1)\n"
    )
    rc = gates.run_gate(store, "T062", ["python3", "-c", code])
    assert rc == 1
    sig = json.loads(store.read_text())["T062"]["gate"]["failure_sig"]
    assert "needle" in sig


# ── v3.15 Stream 2: proof artifact ──────────────────────────────────────────

def _seed_green(store, task_id="T100"):
    gates.run_gate(store, task_id, ["python3", "-c", "print('1 passed')"])


def test_proof_artifact_all_green_is_go(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    _seed_green(store, "T101")
    art = gates.proof_artifact(store, "run-1", ["T100", "T101"])
    assert art["run_id"] == "run-1"
    assert art["verdict"] == "go"
    assert len(art["claims"]) == 2
    for c in art["claims"]:
        assert c["exit_code"] == 0
        assert c["kind"] == "live"           # run_gate-executed evidence
        assert len(c["log_sha256"]) == 64    # hex digest of stored log material
        assert c["evidence_cmd"]


def test_proof_artifact_failing_gate_is_no_go(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    gates.run_gate(store, "T102", ["python3", "-c", "import sys; print('FAILED x'); sys.exit(1)"])
    art = gates.proof_artifact(store, "run-2", ["T100", "T102"])
    assert art["verdict"] == "no-go"


def test_proof_artifact_missing_evidence_is_no_go_and_named(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    art = gates.proof_artifact(store, "run-3", ["T100", "T999"])
    assert art["verdict"] == "no-go"
    assert "T999" in art["missing"]


def test_proof_artifact_caller_evidence_is_structural_and_strict_no_go(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "T103", exit_code=0, cmd="pytest -q")
    art = gates.proof_artifact(store, "run-4", ["T103"])
    assert art["claims"][0]["kind"] == "structural"   # caller-recorded, not runner-proven
    assert art["verdict"] == "go"                      # lax mode still accepts
    strict_art = gates.proof_artifact(store, "run-4", ["T103"], strict=True)
    assert strict_art["verdict"] == "no-go"            # strict rejects structural


def test_proof_artifact_deferrals_named_never_silent(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    art = gates.proof_artifact(store, "run-5", ["T100"],
                               deferrals=["live-telegram-send: no staging bot this run"])
    assert art["verdict"] == "go"
    assert art["deferrals"] == ["live-telegram-send: no staging bot this run"]


def test_cli_proof_writes_artifact_and_exit_codes(tmp_path, capsys, monkeypatch) -> None:
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    monkeypatch.setenv("GATES_STORE", str(store))
    rc = gates.main(["proof", "run-6", "T100"])
    out = capsys.readouterr().out
    assert rc == 0
    artifact = tmp_path / "proof-run-6.json"
    assert artifact.exists()
    assert str(artifact) in out
    data = json.loads(artifact.read_text())
    assert data["verdict"] == "go"
    # no-go exits 1
    gates.run_gate(store, "T104", ["python3", "-c", "import sys; print('FAILED y'); sys.exit(1)"])
    rc = gates.main(["proof", "run-7", "T100", "T104"])
    assert rc == 1


def test_proof_artifact_empty_task_ids_is_no_go(tmp_path) -> None:
    """Codex v3.15 round 1 P1: all([]) is True — an empty proof must never
    read as go."""
    store = tmp_path / "evidence.json"
    art = gates.proof_artifact(store, "run-8", [])
    assert art["verdict"] == "no-go"


def test_cli_proof_out_flag_value_not_treated_as_task_id(tmp_path, capsys, monkeypatch) -> None:
    """Codex v3.15 round 1 P2: --out VALUE must be consumed as a flag value,
    not collected as a task id (which forced a false no-go)."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    monkeypatch.setenv("GATES_STORE", str(store))
    out = tmp_path / "custom-proof.json"
    rc = gates.main(["proof", "run-9", "T100", "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["verdict"] == "go"
    assert data["missing"] == []


def test_cli_proof_run_id_path_traversal_sanitized(tmp_path, monkeypatch) -> None:
    """Codex v3.15 round 2 P2: run_id is argv-controlled — a '../' run id must
    not escape the store dir when composing the default artifact path."""
    store = tmp_path / "sub" / "evidence.json"
    _seed_green(store, "T100")
    monkeypatch.setenv("GATES_STORE", str(store))
    escape = tmp_path / "pwn.json"
    rc = gates.main(["proof", "../pwn", "T100"])
    assert rc == 0
    assert not escape.exists()                      # nothing written outside
    written = list((tmp_path / "sub").glob("proof-*.json"))
    assert len(written) == 1                        # sanitized name, in store dir


def test_proof_artifact_unrecorded_deferral_is_no_go(tmp_path) -> None:
    """Codex v3.15 round 2 P2: a deferral named on argv but absent from
    residuals.md is a silent pass — must force no-go."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    art = gates.proof_artifact(store, "run-10", ["T100"],
                               deferrals=["live-send: no bot"],
                               residuals_text="")
    assert art["verdict"] == "no-go"
    assert art["unrecorded_deferrals"] == ["live-send: no bot"]
    ok = gates.proof_artifact(store, "run-10", ["T100"],
                              deferrals=["live-send: no bot"],
                              residuals_text="- [ ] 2026-07-03 live-send: no bot (run run-10)")
    assert ok["verdict"] == "go"


def test_cli_proof_trailing_defer_is_usage_error(tmp_path, capsys, monkeypatch) -> None:
    """Codex v3.15 round 2 P3: trailing --defer must be a usage error (exit 2),
    not an IndexError crash."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    monkeypatch.setenv("GATES_STORE", str(store))
    rc = gates.main(["proof", "run-11", "T100", "--defer"])
    assert rc == 2
    assert not list(tmp_path.glob("proof-*.json"))  # nothing written


def test_proof_deferral_name_match_is_exact_token_not_substring(tmp_path) -> None:
    """Codex v3.15 round 3 P1: 'live-send' must NOT count as recorded when
    residuals.md only mentions 'live-send-old'."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    art = gates.proof_artifact(
        store, "run-12", ["T100"], deferrals=["live-send: reason"],
        residuals_text="- [ ] 2026-07-03 live-send-old: reason (run run-12)")
    assert art["verdict"] == "no-go"
    assert art["unrecorded_deferrals"] == ["live-send: reason"]
    # exact token still recorded
    ok = gates.proof_artifact(
        store, "run-12", ["T100"], deferrals=["live-send: reason"],
        residuals_text="- [ ] 2026-07-03 live-send: reason (run run-12)")
    assert ok["verdict"] == "go"


def test_cli_proof_flag_value_cannot_be_another_flag(tmp_path, monkeypatch) -> None:
    """Codex v3.15 round 3 P2: '--defer --strict' / '--out --strict' must be
    usage errors, not silently consume the next flag as the value."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    monkeypatch.setenv("GATES_STORE", str(store))
    assert gates.main(["proof", "run-13", "T100", "--defer", "--strict"]) == 2
    assert gates.main(["proof", "run-13", "T100", "--out", "--strict"]) == 2
    assert not list(tmp_path.glob("proof-*.json"))
    assert not (tmp_path / "--strict").exists()


def test_proof_deferral_must_match_current_run_id(tmp_path) -> None:
    """Codex v3.15 round 4 P1: a stale residual from an EARLIER run must not
    satisfy the current run's deferral record."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    stale = "- [ ] 2026-07-01 live-send: reason (run run-OLD)"
    art = gates.proof_artifact(store, "run-14", ["T100"],
                               deferrals=["live-send: reason"],
                               residuals_text=stale)
    assert art["verdict"] == "no-go"
    current = stale + "\n- [ ] 2026-07-03 live-send: reason (run run-14)"
    ok = gates.proof_artifact(store, "run-14", ["T100"],
                              deferrals=["live-send: reason"],
                              residuals_text=current)
    assert ok["verdict"] == "go"


def test_cli_proof_rejects_missing_run_id_and_unknown_flags(tmp_path, monkeypatch) -> None:
    """Codex v3.15 round 4 P2s: no run id → exit 2 (not IndexError); unknown
    options (--stric typo, -h) → exit 2, never silently ignored or collected
    as task ids."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    monkeypatch.setenv("GATES_STORE", str(store))
    assert gates.main(["proof"]) == 2
    assert gates.main(["proof", "run-15", "T100", "--stric"]) == 2
    assert gates.main(["proof", "run-15", "-h"]) == 2
    assert not list(tmp_path.glob("proof-*.json"))


def test_proof_residual_match_is_structural_not_freeform(tmp_path) -> None:
    """Codex v3.15 round 5 P2: the deferral token appearing in another
    record's REASON text must not count as a record for that name."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    spoof = "- [ ] 2026-07-03 note: live-send mentioned in passing (run run-16)"
    art = gates.proof_artifact(store, "run-16", ["T100"],
                               deferrals=["live-send: reason"],
                               residuals_text=spoof)
    assert art["verdict"] == "no-go"
    real = "- [ ] 2026-07-03 live-send: reason (run run-16)"
    ok = gates.proof_artifact(store, "run-16", ["T100"],
                              deferrals=["live-send: reason"],
                              residuals_text=real)
    assert ok["verdict"] == "go"


def test_proof_blank_deferral_name_is_no_go(tmp_path) -> None:
    """Codex v3.15 round 5 P2: --defer '' / '   ' / ': reason' must be
    invalid (no-go), not silently dropped."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    for bad in ["", "   ", ": reason"]:
        art = gates.proof_artifact(store, "run-17", ["T100"],
                                   deferrals=[bad], residuals_text="")
        assert art["verdict"] == "no-go", f"blank deferral {bad!r} slipped through"
        assert bad in art["unrecorded_deferrals"]


def test_proof_residual_for_current_run_must_be_echoed_in_deferrals(tmp_path) -> None:
    """Codex v3.15 round 6 P1: a current-run residual NOT echoed via --defer
    must surface (unechoed_residuals) and force no-go — the artifact may not
    claim go while residuals.md records risk for this run."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    rec = "- [ ] 2026-07-03 live-send: no bot (run run-18)"
    art = gates.proof_artifact(store, "run-18", ["T100"],
                               deferrals=[], residuals_text=rec)
    assert art["verdict"] == "no-go"
    assert art["unechoed_residuals"] == ["live-send"]
    ok = gates.proof_artifact(store, "run-18", ["T100"],
                              deferrals=["live-send: no bot"], residuals_text=rec)
    assert ok["verdict"] == "go"


def test_cli_proof_write_does_not_follow_symlink(tmp_path, monkeypatch) -> None:
    """Codex v3.15 round 6 P2: a pre-planted symlink at the default artifact
    path must not redirect the write to an arbitrary target file."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    monkeypatch.setenv("GATES_STORE", str(store))
    target = tmp_path / "victim.txt"
    target.write_text("original")
    link = tmp_path / "proof-run-19.json"
    link.symlink_to(target)
    rc = gates.main(["proof", "run-19", "T100"])
    assert rc == 0
    assert target.read_text() == "original"          # victim untouched
    assert json.loads((tmp_path / "proof-run-19.json").read_text())["verdict"] == "go"


def test_proof_checked_residual_does_not_satisfy_live_deferral(tmp_path) -> None:
    """Codex v3.15 round 7 P2: a CLOSED checklist item ('- [x]') is not a live
    residual record — only the unchecked form counts."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    closed = "- [x] 2026-07-03 live-send: no bot (run run-20)"
    art = gates.proof_artifact(store, "run-20", ["T100"],
                               deferrals=["live-send: no bot"],
                               residuals_text=closed)
    assert art["verdict"] == "no-go"


def test_cli_proof_sanitized_run_ids_do_not_collide(tmp_path, monkeypatch) -> None:
    """Codex v3.15 round 7 P2: distinct unsafe run ids ('a/b' vs 'a?b') must
    not collapse to the same artifact filename."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    monkeypatch.setenv("GATES_STORE", str(store))
    assert gates.main(["proof", "a/b", "T100"]) == 0
    assert gates.main(["proof", "a?b", "T100"]) == 0
    files = sorted(p.name for p in tmp_path.glob("proof-*.json"))
    assert len(files) == 2, files


def test_proof_duplicate_task_ids_is_no_go(tmp_path) -> None:
    """Codex v3.15 round 8 P2: repeated task ids overstate coverage — must
    surface as duplicates and force no-go."""
    store = tmp_path / "evidence.json"
    _seed_green(store, "T100")
    art = gates.proof_artifact(store, "run-21", ["T100", "T100"])
    assert art["verdict"] == "no-go"
    assert art["duplicate_task_ids"] == ["T100"]


# ── v3.17.0: REFUTED outcome (ported from fable-agent-orchestration
#    investigate-before-fix / think-work-try result states) ──────────────────

def test_note_refuted_records_reason(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.note_refuted(store, "T070", "premise wrong: field never null at HEAD") is True
    data = json.loads(store.read_text())
    assert data["T070"]["refuted"]["reason"].startswith("premise wrong")


def test_note_refuted_rejects_blank_reason(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.note_refuted(store, "T070", "") is False
    assert gates.note_refuted(store, "T070", "   ") is False
    assert not store.exists() or "refuted" not in json.loads(store.read_text()).get("T070", {})


def test_verify_done_accepts_refuted_as_completion(tmp_path) -> None:
    """REFUTED = valid close-with-zero-diff outcome; checkbox may flip.
    Adversarial check of the refutation itself lives at review-gate, not here."""
    store = tmp_path / "evidence.json"
    assert gates.verify_done(store, "T071") is False
    gates.note_refuted(store, "T071", "diagnosis wrong")
    assert gates.verify_done(store, "T071") is True
    # strict fails CLOSED on an unconfirmed refutation (codex v3.17 CRITICAL):
    # a bare caller-asserted refuted must not satisfy GATES_STRICT=1
    assert gates.verify_done(store, "T071", strict=True) is False
    gates.confirm_refuted(store, "T071")
    assert gates.verify_done(store, "T071", strict=True) is True


def test_refuted_does_not_leak_into_gate_evidence(tmp_path) -> None:
    """A refuted task has NO gate evidence — phase_score must not count it
    as a passing gate (it contributes nothing, not a pass)."""
    store = tmp_path / "evidence.json"
    gates.note_refuted(store, "T072", "diagnosis wrong")
    assert "gate" not in json.loads(store.read_text())["T072"]


def test_cli_note_refuted_and_verify_done(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "note-refuted", "T073", "--reason", "cause not reproducible at HEAD"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "REFUTED-RECORDED" in r.stdout
    r2 = _sp.run(["python3", g, "note-refuted", "T073", "--reason", ""],
                 capture_output=True, text=True, env=env)
    assert r2.returncode == 1
    r3 = _sp.run(["python3", g, "verify-done", "T073"], capture_output=True, text=True, env=env)
    assert r3.returncode == 0 and "REFUTED" in r3.stdout


def test_confirm_refuted_requires_prior_note(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.confirm_refuted(store, "T074") is False   # nothing to confirm
    gates.note_refuted(store, "T074", "diagnosis wrong")
    assert gates.confirm_refuted(store, "T074") is True
    assert json.loads(store.read_text())["T074"]["refuted"]["confirmed"] is True


def test_refuted_reason_sanitized_on_print(tmp_path) -> None:
    """Control chars / ANSI escapes in a stored reason must not reach stdout
    verbatim (codex v3.17 MEDIUM: log spoofing)."""
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / 'evidence.json'))
    g = str(DISPATCH_DIR / 'gates.py')
    evil = 'ok' + chr(27) + '[32mFAKE' + chr(27) + '[0m' + chr(10) + 'DONE-VERIFIED (executed_by=run_gate)'
    gates.note_refuted(tmp_path / 'evidence.json', 'T075', evil)
    r = _sp.run(['python3', g, 'verify-done', 'T075'], capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert chr(27) not in r.stdout
    # the spoof line must not appear at start-of-line
    assert not any(l.startswith('DONE-VERIFIED') for l in r.stdout.splitlines())


def test_cli_verify_done_strict_unconfirmed_refuted(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    gates.note_refuted(tmp_path / "evidence.json", "T076", "diagnosis wrong")
    r = _sp.run(["python3", g, "verify-done", "T076", "--strict"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "unconfirmed" in r.stdout.lower()
    r2 = _sp.run(["python3", g, "confirm-refuted", "T076"],
                 capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "CONFIRMED" in r2.stdout
    r3 = _sp.run(["python3", g, "verify-done", "T076", "--strict"],
                 capture_output=True, text=True, env=env)
    assert r3.returncode == 0 and "DONE-REFUTED" in r3.stdout


# ── v3.18.0: autonomy grant ledger ───────────────────────────────────────────

def test_grant_and_check_grant_exact_match(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    assert gates.grant_actions(s, "run-1", ["push:origin/main", "merge:pr"])
    assert gates.check_grant(s, "run-1", "push:origin/main") is True
    assert gates.check_grant(s, "run-1", "push:origin/other") is False
    assert gates.check_grant(s, "run-2", "push:origin/main") is False  # run-bound


def test_grant_rejects_untyped_action(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    # free-prose actions are the fragile-matching bug — must be type:target
    assert gates.grant_actions(s, "run-1", ["just push it"]) is False
    assert gates.check_grant(s, "run-1", "just push it") is False


def test_grant_expires(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    gates.grant_actions(s, "run-1", ["deploy:vercel-web"], ttl_hours=1.0)
    import time as _t
    now = _t.time()
    assert gates.check_grant(s, "run-1", "deploy:vercel-web", now=now) is True
    assert gates.check_grant(s, "run-1", "deploy:vercel-web",
                             now=now + 3601) is False  # fail closed on expiry


def test_pending_records_unlisted_gate(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    gates.record_pending(s, "run-1", "rotate:prod-secret", "not in ledger")
    got = gates.list_pending(s, "run-1")
    assert len(got) == 1 and got[0]["action"] == "rotate:prod-secret"
    # granting it later clears pending
    gates.grant_actions(s, "run-1", ["rotate:prod-secret"])
    assert gates.list_pending(s, "run-1") == []


def test_cli_grant_check_pending(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "grant", "run-9", "--action", "push:origin/main",
                 "--action", "merge:pr", "--ttl-hours", "12"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "GRANTED" in r.stdout
    r2 = _sp.run(["python3", g, "check-grant", "run-9", "--action", "push:origin/main"],
                 capture_output=True, text=True, env=env)
    assert r2.returncode == 0
    r3 = _sp.run(["python3", g, "check-grant", "run-9", "--action", "deploy:prod"],
                 capture_output=True, text=True, env=env)
    assert r3.returncode == 1 and "NOT-GRANTED" in r3.stdout
    r4 = _sp.run(["python3", g, "pending", "run-9", "--action", "deploy:prod",
                  "--reason", "hit unlisted gate"],
                 capture_output=True, text=True, env=env)
    assert r4.returncode == 0
    r5 = _sp.run(["python3", g, "pending", "run-9"],
                 capture_output=True, text=True, env=env)
    assert r5.returncode == 1 and "deploy:prod" in r5.stdout  # exit 1 = pendings exist


# ── v3.18.0: preflight ───────────────────────────────────────────────────────

def test_preflight_env_and_probe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FFS_TEST_PRESENT", "value-should-never-print")
    monkeypatch.delenv("FFS_TEST_MISSING", raising=False)
    res = gates.preflight_check([
        {"kind": "env", "name": "FFS_TEST_PRESENT"},
        {"kind": "env", "name": "FFS_TEST_MISSING"},
        {"kind": "probe", "name": "true-probe", "cmd": "true"},
        {"kind": "probe", "name": "false-probe", "cmd": "false"},
    ])
    assert res["pass"] is False
    by = {r["name"]: r for r in res["results"]}
    assert by["FFS_TEST_PRESENT"]["ok"] and not by["FFS_TEST_MISSING"]["ok"]
    assert by["true-probe"]["ok"] and not by["false-probe"]["ok"]
    # secret value must never appear anywhere in the result
    assert "value-should-never-print" not in json.dumps(res)


def test_preflight_empty_requirements_fails(tmp_path) -> None:
    # empty manifest must not read as pass — all([]) is True hazard
    res = gates.preflight_check([])
    assert res["pass"] is False


def test_cli_preflight_records_and_check(tmp_path, monkeypatch) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"),
               FFS_PF_OK="1")
    g = str(DISPATCH_DIR / "gates.py")
    manifest = tmp_path / "preflight.json"
    manifest.write_text(json.dumps([
        {"kind": "env", "name": "FFS_PF_OK"},
        {"kind": "probe", "name": "echo", "cmd": "true"},
    ]))
    r = _sp.run(["python3", g, "preflight", str(manifest), "--run", "run-9"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "PREFLIGHT-PASS" in r.stdout
    r2 = _sp.run(["python3", g, "check-preflight", "run-9"],
                 capture_output=True, text=True, env=env)
    assert r2.returncode == 0
    # unknown run fails closed
    r3 = _sp.run(["python3", g, "check-preflight", "run-none"],
                 capture_output=True, text=True, env=env)
    assert r3.returncode == 1


def test_cli_preflight_fails_on_missing_env(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = {k: v for k, v in _os.environ.items() if k != "FFS_PF_MISSING"}
    env["GATES_STORE"] = str(tmp_path / "evidence.json")
    g = str(DISPATCH_DIR / "gates.py")
    manifest = tmp_path / "preflight.json"
    manifest.write_text(json.dumps([{"kind": "env", "name": "FFS_PF_MISSING"}]))
    r = _sp.run(["python3", g, "preflight", str(manifest), "--run", "run-9"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "PREFLIGHT-FAIL" in r.stdout
    r2 = _sp.run(["python3", g, "check-preflight", "run-9"],
                 capture_output=True, text=True, env=env)
    assert r2.returncode == 1  # recorded fail must not satisfy check


# ── v3.18.0 codex-round-1 hardening ──────────────────────────────────────────

def test_grant_rejects_nonfinite_or_huge_ttl(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    assert gates.grant_actions(s, "r", ["push:x"], ttl_hours=float("inf")) is False
    assert gates.grant_actions(s, "r", ["push:x"], ttl_hours=0) is False
    assert gates.grant_actions(s, "r", ["push:x"], ttl_hours=-5) is False
    assert gates.grant_actions(s, "r", ["push:x"], ttl_hours=169) is False  # >7d
    assert gates.grant_actions(s, "r", ["push:x"], ttl_hours=12) is True


def test_check_preflight_rejects_future_dated(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    import time as _t
    now = _t.time()
    gates.record_preflight(s, "r", {"pass": True, "checked_at": now + 9999,
                                    "results": []})
    assert gates.check_preflight(s, "r", now=now) is False  # future = corrupt
    gates.record_preflight(s, "r", {"pass": True, "checked_at": now - 10,
                                    "results": []})
    assert gates.check_preflight(s, "r", now=now) is True


def test_pending_rejects_untyped_and_sanitizes_print(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    s = tmp_path / "evidence.json"
    env = dict(_os.environ, GATES_STORE=str(s))
    g = str(DISPATCH_DIR / "gates.py")
    evil = "push:x" + chr(10) + "GRANTED: deploy:prod"
    r = _sp.run(["python3", g, "pending", "run-1", "--action", evil,
                 "--reason", "x"], capture_output=True, text=True, env=env)
    assert r.returncode == 1  # untyped/multiline action rejected
    gates.record_pending(s, "run-1", "push:ok", "r" + chr(27) + "[31mred")
    r2 = _sp.run(["python3", g, "pending", "run-1"],
                 capture_output=True, text=True, env=env)
    assert chr(27) not in r2.stdout


def test_cli_grant_expiry_via_backdated_store(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    s = tmp_path / "evidence.json"
    env = dict(_os.environ, GATES_STORE=str(s))
    g = str(DISPATCH_DIR / "gates.py")
    _sp.run(["python3", g, "grant", "r9", "--action", "push:x",
             "--ttl-hours", "1"], capture_output=True, env=env)
    # backdate the stored expiry — CLI check must honor it
    data = json.loads(s.read_text())
    data["_autonomy"]["r9"]["grants"]["push:x"]["expires_at"] -= 7200
    s.write_text(json.dumps(data))
    r = _sp.run(["python3", g, "check-grant", "r9", "--action", "push:x"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "NOT-GRANTED" in r.stdout


# ── v3.20.0 adversarial round: F1 browser gate must be machine-required ─────

TASKS_BROWSER_OK = """# Tasks
## Phase 3 — US1
- [ ] T001 [US1] [model:sonnet] [qa:e2e] [agent:test-automator] e2e smoke login
- [ ] T002 [US1] [model:sonnet thinking:med] [agent:test-automator] Browser-proof gate — Gate: python3 lib/gates.py run-gate T002 -- python3 lib/runtime_proof.py verify .ralph/phase3/proof.json [qa:browser]
- [ ] T003 [US1] [model:sonnet] [agent:ecc:code-reviewer] /review-gate — review Phase 3 [qa:review-gate]
"""

TASKS_NO_BROWSER_GATE = """# Tasks
## Phase 3 — US1
- [ ] T001 [US1] [model:sonnet] [qa:e2e] [agent:test-automator] e2e smoke login
- [ ] T002 [US1] [model:sonnet] [agent:ecc:code-reviewer] /review-gate — review Phase 3 [qa:review-gate]
"""

TASKS_BROWSER_TAG_NO_VERIFY = """# Tasks
## Phase 3 — US1
- [ ] T001 [US1] [model:sonnet] [qa:e2e] [agent:test-automator] e2e smoke login
- [ ] T002 [US1] [model:sonnet] [agent:test-automator] browser things [qa:browser]
- [ ] T003 [US1] [model:sonnet] [agent:ecc:code-reviewer] /review-gate — review Phase 3 [qa:review-gate]
"""

SPEC_ONE_STORY = "# Spec\n## US1: login\n"


def test_analyze_requires_browser_gate_when_scenarios_exist() -> None:
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, TASKS_NO_BROWSER_GATE,
                                       has_scenarios=True)
    assert any("qa:browser" in f for f in findings)


def test_analyze_browser_gate_satisfies_rule() -> None:
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, TASKS_BROWSER_OK,
                                       has_scenarios=True)
    assert findings == []


def test_analyze_browser_tag_without_runtime_proof_verify_flagged() -> None:
    findings = gates.analyze_artifacts(SPEC_ONE_STORY,
                                       TASKS_BROWSER_TAG_NO_VERIFY,
                                       has_scenarios=True)
    assert any("runtime_proof" in f for f in findings)


def test_analyze_browser_tag_without_scenarios_flagged() -> None:
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, TASKS_BROWSER_OK,
                                       has_scenarios=False)
    assert any("scenarios.md" in f for f in findings)


def test_analyze_no_scenarios_no_browser_tasks_clean() -> None:
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, TASKS_NO_BROWSER_GATE,
                                       has_scenarios=False)
    assert findings == []


def test_analyze_browser_gate_substring_spoof_rejected() -> None:
    # codex round: bare "runtime_proof" substring must not satisfy the rule —
    # the line must carry an actual runtime_proof.py verify gate command
    tasks = """# Tasks
## Phase 3 — US1
- [ ] T001 [US1] [qa:e2e] e2e smoke login
- [ ] T002 [US1] [qa:browser] browser stuff (runtime_proof later?)
- [ ] T003 [US1] /review-gate [qa:review-gate]
"""
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, tasks,
                                       has_scenarios=True)
    assert any("runtime_proof" in f for f in findings)


# codex round 3 H1: browser-touching tasks with NO scenarios.md must not
# slide through analyze — bypass-by-omission hole. Web-touch is detected
# from web-surface file paths in the tasks text itself.

TASKS_WEB_TOUCH_NO_BROWSER = """# Tasks
## Phase 3 — US1
- [ ] T001 [US1] [model:sonnet] [agent:frontend-developer] build web/src/components/Nav.tsx
- [ ] T002 [US1] [model:sonnet] [qa:e2e] [agent:test-automator] e2e smoke login
- [ ] T003 [US1] [model:sonnet] [agent:ecc:code-reviewer] /review-gate — review Phase 3 [qa:review-gate]
"""


def test_analyze_web_paths_without_scenarios_flagged() -> None:
    findings = gates.analyze_artifacts(SPEC_ONE_STORY,
                                       TASKS_WEB_TOUCH_NO_BROWSER,
                                       has_scenarios=False)
    assert any("scenarios.md" in f for f in findings)


def test_analyze_web_paths_with_scenarios_and_gate_clean() -> None:
    tasks = TASKS_BROWSER_OK + (
        "- [ ] T009 [US1] [model:haiku] [agent:frontend-developer] "
        "tweak web/src/components/Nav.tsx\n")
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, tasks,
                                       has_scenarios=True)
    assert findings == []


def test_analyze_non_web_tasks_without_scenarios_clean() -> None:
    tasks = TASKS_NO_BROWSER_GATE + (
        "- [ ] T009 [US1] [model:haiku] [agent:ecc:python-reviewer] "
        "refactor lib/gates.py internals\n")
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, tasks,
                                       has_scenarios=False)
    assert findings == []
