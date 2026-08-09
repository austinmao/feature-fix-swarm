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


# ── spec-008 Phase 2: durable operator waivers (RED) ───────────────────────

def test_waiver_rows_are_validated_and_append_without_deduplication(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_waiver(store, "spec-008", "canary-gate", "CANARY_GATE=off")
    gates.record_waiver(store, "spec-008", "canary-gate", "CANARY_GATE=off")
    rows = json.loads(store.read_text())["waivers"]
    assert len(rows) == 2
    assert {"run_id", "gate", "env_var", "ts"} == set(rows[0])


def test_waiver_rejects_blank_or_oversized_values_without_partial_row(tmp_path) -> None:
    import pytest

    store = tmp_path / "evidence.json"
    with pytest.raises(ValueError, match="INVALID-WAIVER"):
        gates.record_waiver(store, "", "canary-gate", "CANARY_GATE=off")
    with pytest.raises(ValueError, match="INVALID-WAIVER"):
        gates.record_waiver(store, "spec-008", "x" * 257, "CANARY_GATE=off")
    assert not store.exists()


# ── spec-008 Phase 1: degradation evidence + ratio guard (RED) ─────────────

def test_degradation_events_are_validated_idempotent_and_ratio_scoped(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.note_degraded(store, "rung-attempt", rung_id="vendor:model:high",
                               outcome="fail") is True
    assert gates.note_degraded(store, "invocation", run_id="spec-008",
                               seam="review-gate", degraded=True,
                               invocation_id="review-1") is True
    assert gates.note_degraded(store, "invocation", run_id="spec-008",
                               seam="review-gate", degraded=True,
                               invocation_id="review-1") is False
    assert gates.degraded_ratio(store, "spec-008") == (1, 1)
    try:
        gates.note_degraded(store, "invocation", run_id="spec-008",
                             seam="review-gate", degraded=False,
                             invocation_id="review-1")
    except ValueError as exc:
        assert "IDEMPOTENCY-CONFLICT" in str(exc)
    else:
        raise AssertionError("conflicting replay must fail closed")
    try:
        gates.degraded_ratio(store, "not-a-ledger-id")
    except ValueError:
        pass
    else:
        raise AssertionError("ratio reads must reject non-ledger ids")


def test_degradation_rung_status_probe_and_reset_are_locked_contracts(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    rung = "vendor:model:high"
    for _ in range(20):
        gates.note_degraded(store, "rung-attempt", rung_id=rung, outcome="fail")
    assert gates.rung_status(store, rung)["tripped"] is True
    assert [gates.probe_check(store, rung) for _ in range(10)] == [False] * 9 + [True]
    assert gates.reset_rung(store, rung) is True
    assert gates.rung_status(store, rung)["tripped"] is False


def test_run_mapping_is_shape_checked_idempotent_and_conflict_rejecting(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    runstore = "a" * 12
    assert gates.record_run_mapping(store, "spec-008", runstore) is True
    assert gates.record_run_mapping(store, "spec-008", runstore) is False
    import pytest
    with pytest.raises(ValueError, match="RUN-MAPPING-CONFLICT"):
        gates.record_run_mapping(store, "spec-008", "b" * 12)
    with pytest.raises(ValueError, match="INVALID-RUNSTORE-ID"):
        gates.record_run_mapping(store, "spec-009", "not-a-uuid")


def test_run_mapping_is_readable_for_relaunch_reuse(tmp_path) -> None:
    # A relaunch of the same ledger run must be able to READ its existing
    # mapping and reuse the runstore instead of starting a fresh record and
    # dying on RUN-MAPPING-CONFLICT (first phase-2 drive relaunch wedged on
    # exactly this). Missing mapping reads as None, never raises.
    store = tmp_path / "evidence.json"
    assert gates.get_run_mapping(store, "spec-008") is None
    gates.record_run_mapping(store, "spec-008", "a" * 12)
    assert gates.get_run_mapping(store, "spec-008") == "a" * 12
    import pytest
    with pytest.raises(ValueError):
        gates.get_run_mapping(store, "not-ledger-shaped")


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


def test_scan_tamper_exit_zero_fixture_allowlist() -> None:
    # F1 (PR #94 audit): bats stub fixtures and @test titles are not gate
    # bypasses. Exemptions are narrow — the exit-0 class only, and only for
    # @test title lines or lines carrying an explicit `tamper-ok:` note.
    diff = (
        "--- a/tests/bats/x.bats\n"
        "+++ b/tests/bats/x.bats\n"
        '+@test "hook exits 0 when store absent (exit 0 fast path)" {\n'
        "+  printf 'exit 0\\n' > \"$STUBDIR/python3\"  # tamper-ok: stub fixture\n"
    )
    assert gates.scan_test_tampering(diff) == []


def test_scan_tamper_comment_line_exit_zero_not_flagged() -> None:
    # PR #103 CI false positive: doc comments like `+#   --immediate ... exit 0`
    # were flagged — a pure comment line cannot alter control flow.
    diff = (
        "--- a/scripts/gsd/digest.sh\n"
        "+++ b/scripts/gsd/digest.sh\n"
        "+#   --immediate  poll stores, emit new events, exit 0 always\n"  # tamper-ok: quoted scanner fixture
        "+  # cursor retained on notify failure; exit 0\n"  # tamper-ok: quoted scanner fixture
    )
    assert gates.scan_test_tampering(diff) == []


def test_scan_tamper_exit_zero_finding_carries_path() -> None:
    # Findings are path-scoped so the CI tamper job can allowlist files whose
    # CONTRACT is unconditional exit 0 without blinding the heuristic.
    diff = (
        "--- a/scripts/gsd/whatever.sh\n"
        "+++ b/scripts/gsd/whatever.sh\n"
        "+exit 0\n"  # tamper-ok: quoted scanner fixture
    )
    findings = gates.scan_test_tampering(diff)
    assert any("[scripts/gsd/whatever.sh]" in f for f in findings)


def test_scan_tamper_unannotated_exit_zero_in_test_body_still_flags() -> None:
    diff = (
        "--- a/tests/bats/x.bats\n"
        "+++ b/tests/bats/x.bats\n"
        "+  exit 0\n"
    )
    findings = gates.scan_test_tampering(diff)
    assert any("exit 0" in f for f in findings)


def test_scan_tamper_one_liner_test_disable_still_flags() -> None:
    # review-gate round 2 HIGH: `@test "x" { exit 0; }` disables the test —
    # the exit 0 is control flow (outside quotes), so the title exemption
    # must not apply.
    diff = (
        "--- a/tests/bats/x.bats\n"
        "+++ b/tests/bats/x.bats\n"
        '+@test "x" { exit 0; }\n'
    )
    findings = gates.scan_test_tampering(diff)
    assert any("exit 0" in f for f in findings)


def test_scan_tamper_annotated_executable_exit_zero_still_flags() -> None:
    # `exit 0  # tamper-ok:` as a statement (unquoted) is control flow —
    # the annotation only covers exit 0 written as quoted DATA.
    diff = (
        "--- a/tests/bats/x.bats\n"
        "+++ b/tests/bats/x.bats\n"
        "+  exit 0  # tamper-ok: trust me\n"
    )
    findings = gates.scan_test_tampering(diff)
    assert any("exit 0" in f for f in findings)


def test_scan_tamper_annotation_never_exempts_non_test_files() -> None:
    # review-gate HIGH: `exit 0 # tamper-ok:` in a gate/CI/impl script must
    # still flag — the allowlist is test-fixture-only.
    diff = (
        "--- a/scripts/gsd/canary-gate.sh\n"
        "+++ b/scripts/gsd/canary-gate.sh\n"
        "+exit 0  # tamper-ok: nice try\n"
    )
    findings = gates.scan_test_tampering(diff)
    assert any("exit 0" in f for f in findings)


def test_scan_tamper_clean_diff_returns_empty() -> None:
    # neutral path: lib/gates.py itself is now a (warn-tier) finding by
    # design — G2 flags gate-implementation edits; see
    # test_scan_tamper_flags_gate_and_ledger_file_edits.
    diff = (
        "--- a/lib/helpers.py\n"
        "+++ b/lib/helpers.py\n"
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

def test_scan_tamper_flags_gate_and_ledger_file_edits() -> None:
    diff = (
        "--- a/lib/gates.py\n+++ b/lib/gates.py\n+x = 1\n"
        "--- a/.feature-fix-swarm/evidence.json\n"
        "+++ b/.feature-fix-swarm/evidence.json\n+{}\n"
        "--- a/lib/other.py\n+++ b/lib/other.py\n+y = 2\n"
    )
    findings = gates.scan_test_tampering(diff)
    gate_hits = [f for f in findings if f.startswith("gate/ledger file edited")]
    assert len(gate_hits) == 2
    assert not any("other.py" in f for f in gate_hits)


def test_store_path_env_always_wins(monkeypatch) -> None:
    monkeypatch.setenv("GATES_STORE", "/tmp/custom/ev.json")
    assert gates._store_path() == gates.Path("/tmp/custom/ev.json")


def test_store_path_resolves_worktree_to_main_checkout(tmp_path, monkeypatch) -> None:
    """G5: from a linked worktree the DEFAULT store must resolve to the MAIN
    checkout's .feature-fix-swarm/evidence.json, not the worktree's cwd."""
    import subprocess as sp
    main = tmp_path / "main"
    main.mkdir()
    sp.run(["git", "init", "-q", "-b", "main"], cwd=main, check=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "init"], cwd=main, check=True)
    wt = tmp_path / "wt"
    sp.run(["git", "worktree", "add", "-q", str(wt), "-b", "side"],
           cwd=main, check=True)
    monkeypatch.delenv("GATES_STORE", raising=False)
    monkeypatch.chdir(wt)
    resolved = gates._store_path()
    assert resolved == main / ".feature-fix-swarm" / "evidence.json"
    # from the main checkout itself, behavior is the historic relative default
    monkeypatch.chdir(main)
    assert str(gates._store_path()).endswith(".feature-fix-swarm/evidence.json")


def test_no_progress_when_same_failure_repeats() -> None:
    assert gates.no_progress(["FAILED test_a - AssertionError",
                              "FAILED test_a - AssertionError"]) is True
    assert gates.no_progress(["FAILED test_a", "FAILED test_b"]) is False
    assert gates.no_progress(["FAILED test_a"]) is False


def test_no_progress_catches_oscillating_signatures() -> None:
    """2026-08 red-team G3: an A,B,A,B loop never trips a consecutive-pair
    test — the two recorded burn incidents ran 19 and 38 rounds on exactly
    this shape. Revisiting ANY earlier signature is no-progress."""
    assert gates.no_progress(["sig A", "sig B", "sig A"]) is True
    assert gates.no_progress(["sig A", "sig B", "sig C", "sig B"]) is True
    # a genuinely new failure is still progress
    assert gates.no_progress(["sig A", "sig B", "sig C"]) is False


def test_loop_round_counts_durably_and_resets(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.loop_round(store, "run-1", "wall:p1") == 1
    assert gates.loop_round(store, "run-1", "wall:p1") == 2
    # independent loops and runs do not share counters
    assert gates.loop_round(store, "run-1", "wall:p2") == 1
    assert gates.loop_round(store, "run-2", "wall:p1") == 1
    gates.reset_loop_round(store, "run-1", "wall:p1")
    assert gates.loop_round(store, "run-1", "wall:p1") == 1
    # reset-all drops every counter for the run, others untouched
    gates.reset_loop_round(store, "run-1", None)
    assert gates.loop_round(store, "run-1", "wall:p2") == 1
    assert gates.loop_round(store, "run-2", "wall:p1") == 2


def test_reset_loop_round_noop_leaves_store_byte_identical(tmp_path) -> None:
    # run-finalizer sweeps loop-round counters on EVERY landed run; a store
    # with no counters must not be rewritten (run-finalizer.bats pins
    # "evidence store untouched" against the literal prior bytes).
    import json
    store = tmp_path / "evidence.json"
    store.write_text('{"k":"v"}')
    gates.reset_loop_round(store, "run-1", None)
    gates.reset_loop_round(store, "run-1", "wall:p1")
    assert store.read_text() == '{"k":"v"}'
    # a store that has OTHER runs' counters is also untouched by a no-op reset
    gates.loop_round(store, "run-2", "wall:p1")
    before = store.read_text()
    gates.reset_loop_round(store, "run-1", None)
    assert store.read_text() == before
    assert json.loads(before)["k"] == "v"


def test_loop_round_note_count_records_and_returns_prev(tmp_path) -> None:
    """Wall policy (b) (2026-08-08 operator decision): the plan wall passes on
    zero-CRITICAL + strict round-over-round decrease in new HIGH/CRITICAL
    findings. That comparison needs per-round count history stored beside the
    round counter."""
    store = tmp_path / "evidence.json"
    gates.loop_round(store, "run-1", "wall:p1")
    assert gates.loop_round_note_count(store, "run-1", "wall:p1", 7) == (1, None)
    gates.loop_round(store, "run-1", "wall:p1")
    assert gates.loop_round_note_count(store, "run-1", "wall:p1", 4) == (2, 7)
    gates.loop_round(store, "run-1", "wall:p1")
    assert gates.loop_round_note_count(store, "run-1", "wall:p1", 4) == (3, 4)
    # re-noting the SAME round overwrites (idempotent re-run of one round)
    assert gates.loop_round_note_count(store, "run-1", "wall:p1", 2) == (3, 4)
    # independent loops do not share history
    gates.loop_round(store, "run-1", "wall:p2")
    assert gates.loop_round_note_count(store, "run-1", "wall:p2", 9) == (1, None)


def test_loop_round_note_count_requires_active_round(tmp_path) -> None:
    import pytest as _pytest
    store = tmp_path / "evidence.json"
    with _pytest.raises(ValueError):
        gates.loop_round_note_count(store, "run-1", "wall:p1", 3)


def test_reset_loop_round_clears_count_history(tmp_path) -> None:
    """A named reset must drop the count history WITH the round counter —
    a stale pre-reset count would otherwise fake a round-over-round
    decrease on the first post-reset round."""
    store = tmp_path / "evidence.json"
    gates.loop_round(store, "run-1", "wall:p1")
    gates.loop_round_note_count(store, "run-1", "wall:p1", 5)
    gates.reset_loop_round(store, "run-1", "wall:p1")
    gates.loop_round(store, "run-1", "wall:p1")
    # fresh round 1: prev must be None, not the pre-reset round-0 ghost
    assert gates.loop_round_note_count(store, "run-1", "wall:p1", 3) == (1, None)
    # reset-all drops history too
    gates.loop_round_note_count(store, "run-1", "wall:p1", 3)
    gates.reset_loop_round(store, "run-1", None)
    gates.loop_round(store, "run-1", "wall:p1")
    assert gates.loop_round_note_count(store, "run-1", "wall:p1", 1) == (1, None)


def test_reset_loop_round_creates_no_debris_on_missing_or_corrupt_store(tmp_path) -> None:
    import pytest as _pytest
    # missing store: reset is a silent no-op — must not resurrect the parent
    # dir or drop a .lock (run-finalizer step 4b runs AFTER worktree removal)
    gone = tmp_path / "removed-worktree" / "custom-gates" / "evidence.json"
    gates.reset_loop_round(gone, "run-1", None)
    assert not gone.parent.exists()
    # corrupt store: the error still surfaces (CLI maps it to rc 3), but no
    # .lock may be created on the way out
    corrupt = tmp_path / "evidence.json"
    corrupt.write_text("GATES-VERSION")
    with _pytest.raises(ValueError):
        gates.reset_loop_round(corrupt, "run-1", None)
    assert not (tmp_path / "evidence.lock").exists()


def test_loop_round_shape_guard_refuses_conflicting_store(tmp_path) -> None:
    import json
    store = tmp_path / "evidence.json"
    store.write_text(json.dumps({"_loops": "not-a-dict"}))
    import pytest
    with pytest.raises(SystemExit):
        gates.loop_round(store, "run-1", "wall:p1")


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


def test_grant_default_ttl_covers_multiday_runs(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    # No explicit ttl_hours: default must cover multi-day agentic runs (72h)
    # — 24h grants expired mid-run on overnight+next-day work.
    gates.grant_actions(s, "run-1", ["ship:gsd"])
    import time as _t
    now = _t.time()
    assert gates.check_grant(s, "run-1", "ship:gsd",
                             now=now + 71 * 3600) is True
    assert gates.check_grant(s, "run-1", "ship:gsd",
                             now=now + 73 * 3600) is False


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
        {"kind": "probe", "name": "true-probe", "argv": ["true"]},
        {"kind": "probe", "name": "false-probe", "argv": ["false"]},
    ])
    assert res["pass"] is False
    by = {r["name"]: r for r in res["results"]}
    assert by["FFS_TEST_PRESENT"]["ok"] and not by["FFS_TEST_MISSING"]["ok"]
    assert by["true-probe"]["ok"] and not by["false-probe"]["ok"]
    # secret value must never appear anywhere in the result
    assert "value-should-never-print" not in json.dumps(res)


def test_preflight_probe_rejects_shell_command_strings(tmp_path) -> None:
    marker = tmp_path / "shell-command-ran"
    res = gates.preflight_check([
        {
            "kind": "probe",
            "name": "legacy-shell-command",
            "cmd": f"touch {marker}",
        },
    ])

    assert res["pass"] is False
    assert res["results"][0]["detail"] == "INVALID: probe requires a non-empty argv string array"
    assert not marker.exists()


def test_preflight_probe_rejects_invalid_argv_shapes() -> None:
    for argv in (None, [], "true", ["true", 1], [""]):
        res = gates.preflight_check([
            {"kind": "probe", "name": "invalid-argv", "argv": argv},
        ])
        assert res["pass"] is False
        assert res["results"][0]["detail"].startswith("INVALID:")


def test_preflight_probe_inherits_environment_without_recording_value(monkeypatch) -> None:
    secret = "postgresql://private-value"
    monkeypatch.setenv("FFS_TEST_DATABASE_URL", secret)
    res = gates.preflight_check([
        {
            "kind": "probe",
            "name": "database",
            "argv": [
                "python3",
                "-c",
                "import os,sys; sys.exit(0 if os.environ.get('FFS_TEST_DATABASE_URL') else 1)",
            ],
        },
    ])

    assert res["pass"] is True
    assert secret not in json.dumps(res)


def test_preflight_probe_rejects_environment_placeholders(monkeypatch) -> None:
    def unexpected_run(command, **kwargs):
        raise AssertionError(f"placeholder command executed: {command}")

    monkeypatch.setattr(gates.subprocess, "run", unexpected_run)
    for placeholder in ("$DATABASE_URL", "${DATABASE_URL}"):
        res = gates.preflight_check([
            {"kind": "probe", "name": "database", "argv": ["psql", placeholder]},
        ])
        assert res["pass"] is False
        assert res["results"][0]["detail"] == (
            "INVALID: environment placeholders are not allowed in probe argv"
        )


def test_preflight_probe_reports_timeout_without_raising(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        raise gates.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(gates.subprocess, "run", fake_run)
    res = gates.preflight_check([
        {"kind": "probe", "name": "slow", "argv": ["slow-command"]},
    ], timeout=7)

    assert res["pass"] is False
    assert res["results"][0]["detail"] == "timeout after 7s"


def test_preflight_probe_reports_missing_executable_without_raising(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(gates.subprocess, "run", fake_run)
    res = gates.preflight_check([
        {"kind": "probe", "name": "missing", "argv": ["missing-command"]},
    ])

    assert res["pass"] is False
    assert res["results"][0]["detail"] == "executable unavailable"


def test_shipped_preflight_manifest_uses_valid_probe_argv() -> None:
    manifest = json.loads(
        (DISPATCH_DIR.parent / "specs/003-orchestration-hardening/preflight.json")
        .read_text(encoding="utf-8")
    )

    probes = [requirement for requirement in manifest if requirement.get("kind") == "probe"]
    assert probes
    for probe in probes:
        assert "cmd" not in probe
        assert isinstance(probe.get("argv"), list)
        assert probe["argv"]
        assert all(isinstance(arg, str) and arg and "\0" not in arg for arg in probe["argv"])
        assert not any(arg == ".planning" or arg.startswith(".planning/") for arg in probe["argv"])


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
        {"kind": "probe", "name": "echo", "argv": ["true"]},
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


# codex round 4: WEB_TASK_PATH_RE must match the browser-proof.sh WEB_RE
# contract — hooks/stores/styles dirs and app/ api/ route files are
# web-touch too; a plan naming only those paths must still demand
# scenarios.md.

def test_analyze_hooks_dir_path_without_scenarios_flagged() -> None:
    tasks = TASKS_NO_BROWSER_GATE + (
        "- [ ] T009 [US1] [model:sonnet] [agent:frontend-developer] "
        "add web/src/hooks/useCart.ts\n")
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, tasks,
                                       has_scenarios=False)
    assert any("scenarios.md" in f for f in findings)


def test_analyze_app_api_route_path_without_scenarios_flagged() -> None:
    tasks = TASKS_NO_BROWSER_GATE + (
        "- [ ] T009 [US1] [model:sonnet] [agent:nextjs-backend-engineer] "
        "add web/src/app/api/cart/route.ts\n")
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, tasks,
                                       has_scenarios=False)
    assert any("scenarios.md" in f for f in findings)


def test_analyze_api_prose_mention_not_a_path_clean() -> None:
    tasks = TASKS_NO_BROWSER_GATE + (
        "- [ ] T009 [US1] [model:sonnet] [agent:ecc:python-reviewer] "
        "document the api/ contract in prose\n")
    findings = gates.analyze_artifacts(SPEC_ONE_STORY, tasks,
                                       has_scenarios=False)
    assert findings == []


# ── spec-295 Phase 1: REQ-05 backward-compat regression (RED-first) ─────────
# This is the standing guard that protects every other spec: it goes green
# immediately on current (unmodified) code and MUST go RED the instant a
# future change adds an unconditional record_pending or any other non-prod
# check_grant store mutation. Written and green BEFORE any gates.py edit.

def test_backward_compat_non_prod_action_no_promotions(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.grant_actions(store, "run-1", ["push:origin/main"])
    # LOAD-BEARING assertion (adversary MEDIUM #7): exact byte-identical
    # snapshot around a non-prod refusal — catches ANY rewrite or mutation,
    # not just an added pending record.
    before = store.read_bytes()
    assert gates.check_grant(store, "run-1", "push:origin/other") is False
    assert store.read_bytes() == before
    # supplementary assertions
    assert gates.list_pending(store, "run-1") == []
    assert "_promotions" not in json.loads(store.read_text())
    assert gates.check_grant(store, "run-1", "push:origin/main") is True


# ── spec-295 Phase 1: record_promotion (RED → GREEN) ─────────────────────────

_GOOD_ARTIFACT = "myapp@sha256:" + "f" * 64


def _seed_success(store: Path, task_id: str = "stg-web", *,
                  artifact: str = _GOOD_ARTIFACT) -> None:
    assert gates.run_gate(
        store, task_id, ["python3", "-c", "raise SystemExit(0)"],
        artifact=artifact,
    ) == 0


def test_record_promotion_persists_with_resolving_evidence_and_digest_artifact(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    ok = gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                                surface="web", artifact=_GOOD_ARTIFACT,
                                evidence_ids=["stg-web"])
    assert ok is True
    data = json.loads(store.read_text())
    assert isinstance(data["_promotions"]["run-1"], list)
    rec = data["_promotions"]["run-1"][0]
    assert rec["from_env"] == "staging" and rec["to_env"] == "prod"
    assert rec["surface"] == "web" and rec["artifact"] == _GOOD_ARTIFACT
    assert rec["evidence_ids"] == ["stg-web"]
    assert isinstance(rec["recorded_at"], float) and isinstance(rec["expires_at"], float)


def test_record_promotion_rejects_unresolved_evidence(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    ok = gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                                surface="web", artifact=_GOOD_ARTIFACT,
                                evidence_ids=["never-ran"])
    assert ok is False
    assert not store.exists() or "_promotions" not in json.loads(store.read_text())


def test_record_promotion_rejects_failed_gate_evidence(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "stg-web", exit_code=1, cmd="pytest -q")
    ok = gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                                surface="web", artifact=_GOOD_ARTIFACT,
                                evidence_ids=["stg-web"])
    assert ok is False
    assert "_promotions" not in json.loads(store.read_text())


def test_record_promotion_persists_bare_commit_sha(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    sha = "d" * 40
    _seed_success(store, artifact=sha)
    ok = gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                                surface="web", artifact=sha, evidence_ids=["stg-web"])
    assert ok is True


def test_record_promotion_persists_digest_pinned_tag(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    artifact = "myapp:v1.2.3@sha256:" + "e" * 64
    _seed_success(store, artifact=artifact)
    ok = gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                                surface="web", artifact=artifact, evidence_ids=["stg-web"])
    assert ok is True


def test_record_promotion_rejects_malformed_inputs(tmp_path) -> None:
    cases = [
        dict(evidence_ids=["never-ran"]),
        dict(evidence_ids="e1"),
        dict(evidence_ids=[]),
        dict(evidence_ids=["", "e2"]),
        dict(evidence_ids=(x for x in [])),
        dict(artifact="img:latest"),
        dict(artifact="img:main"),
        dict(artifact="myapp@sha256:xyz"),
        dict(artifact="g" * 39),
        dict(artifact=_GOOD_ARTIFACT + "\n"),
        dict(artifact=12345),
        dict(ttl_hours="24"),
        dict(ttl_hours=0),
        dict(ttl_hours=-1),
        dict(ttl_hours=float("inf")),
        dict(ttl_hours=gates.GRANT_MAX_TTL_HOURS + 1),
    ]
    for i, overrides in enumerate(cases):
        store = tmp_path / f"case-{i}.json"
        _seed_success(store)
        kwargs = dict(from_env="staging", to_env="prod", surface="web",
                     artifact=_GOOD_ARTIFACT, evidence_ids=["stg-web"])
        kwargs.update(overrides)
        ok = gates.record_promotion(store, "run-1", **kwargs)
        assert ok is False, f"case {i} {overrides} should be rejected"
        data = json.loads(store.read_text())
        assert not data.get("_promotions", {}).get("run-1"), \
            f"case {i} {overrides} wrote a record"


def test_record_promotion_concurrency_survives_with_grant(tmp_path) -> None:
    import threading
    store = tmp_path / "evidence.json"
    n = 5
    for i in range(n):
        artifact = f"myapp@sha256:{i:064x}"
        _seed_success(store, task_id=f"stg-web-{i}", artifact=artifact)
    barrier = threading.Barrier(n + 1)
    results: list[bool] = []
    append_lock = threading.Lock()

    def do_promote(i: int) -> None:
        barrier.wait()
        artifact = f"myapp@sha256:{i:064x}"
        ok = gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                                    surface="web", artifact=artifact,
                                    evidence_ids=[f"stg-web-{i}"])
        with append_lock:
            results.append(ok)

    def do_grant() -> None:
        barrier.wait()
        gates.grant_actions(store, "run-1", ["push:origin/main"])

    threads = [threading.Thread(target=do_promote, args=(i,)) for i in range(n)]
    threads.append(threading.Thread(target=do_grant))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results)
    data = json.loads(store.read_text())
    assert len(data["_promotions"]["run-1"]) == n
    assert gates.check_grant(store, "run-1", "push:origin/main") is True


def test_record_promotion_nondict_promotions_raises(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    store.write_text(json.dumps({"_promotions": "not-a-dict"}))
    _seed_success(store)
    try:
        gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                               surface="web", artifact=_GOOD_ARTIFACT,
                               evidence_ids=["stg-web"])
        assert False, "expected SystemExit"
    except SystemExit:
        pass


def test_record_promotion_nonlist_run_entry_raises(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    store.write_text(json.dumps({"_promotions": {"run-1": "not-a-list"}}))
    _seed_success(store)
    try:
        gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                               surface="web", artifact=_GOOD_ARTIFACT,
                               evidence_ids=["stg-web"])
        assert False, "expected SystemExit"
    except SystemExit:
        pass


# ── spec-295 Phase 1: check_promotion — fail-closed read (RED → GREEN) ──────

def test_check_promotion_true_for_fresh_exact_match(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="web", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    import time as _t
    t = _t.time()
    assert gates.check_promotion(store, "run-1", "prod", "web", _GOOD_ARTIFACT,
                                 now=t) is True


def test_check_promotion_false_after_expiry(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    import time as _t
    now = _t.time()
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="web", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"], ttl_hours=1.0)
    assert gates.check_promotion(store, "run-1", "prod", "web", _GOOD_ARTIFACT,
                                 now=now) is True
    assert gates.check_promotion(store, "run-1", "prod", "web", _GOOD_ARTIFACT,
                                 now=now + 3601) is False


def test_check_promotion_false_on_artifact_mismatch(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="web", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    other = "myapp@sha256:" + "9" * 64
    assert gates.check_promotion(store, "run-1", "prod", "web", other) is False


def test_check_promotion_false_on_surface_mismatch(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="web", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_promotion(store, "run-1", "prod", "cp", _GOOD_ARTIFACT) is False


def test_check_promotion_false_on_to_env_mismatch(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="web", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_promotion(store, "run-1", "staging", "web",
                                 _GOOD_ARTIFACT) is False


def test_check_promotion_requires_from_env_staging_for_prod(tmp_path) -> None:
    # adversary CRITICAL #2: only a staging->prod record satisfies a
    # to_env=="prod" precondition — dev->prod / prod->prod never count.
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.record_promotion(store, "run-1", from_env="dev", to_env="prod",
                           surface="web", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_promotion(store, "run-1", "prod", "web",
                                 _GOOD_ARTIFACT) is False


def test_check_promotion_malformed_ledger_fails_closed(tmp_path) -> None:
    good_rec = {"from_env": "staging", "to_env": "prod", "surface": "web",
               "artifact": _GOOD_ARTIFACT, "evidence_ids": ["stg-web"]}
    cases = [
        {"_promotions": "not-a-dict"},
        {"_promotions": {"run-1": "not-a-list"}},
        {"_promotions": {"run-1": [{**good_rec, "recorded_at": 1.0}]}},          # missing expires_at
        {"_promotions": {"run-1": [{**good_rec, "recorded_at": 1.0,
                                    "expires_at": "soon"}]}},                     # non-numeric
        {"_promotions": {"run-1": [{**good_rec, "recorded_at": 1.0,
                                    "expires_at": float("inf")}]}},               # infinite
    ]
    for i, seeded in enumerate(cases):
        store = tmp_path / f"malformed-{i}.json"
        store.write_text(json.dumps(seeded))
        assert gates.check_promotion(store, "run-1", "prod", "web",
                                     _GOOD_ARTIFACT) is False, f"case {i}"


def test_check_promotion_revalidates_artifact_and_runner_provenance_on_read(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    data = json.loads(store.read_text())
    now = gates._now()
    base = {
        "from_env": "staging",
        "to_env": "prod",
        "surface": "web",
        "artifact": _GOOD_ARTIFACT,
        "evidence_ids": ["stg-web"],
        "recorded_at": now,
        "expires_at": now + 3600,
    }
    cases = [
        ({**base, "artifact": "img:latest"}, "img:latest"),
        ({k: v for k, v in base.items() if k != "evidence_ids"}, _GOOD_ARTIFACT),
        ({**base, "evidence_ids": ["never-ran"]}, _GOOD_ARTIFACT),
        ({k: v for k, v in base.items() if k != "recorded_at"}, _GOOD_ARTIFACT),
        ({**base, "expires_at": now + (gates.GRANT_MAX_TTL_HOURS + 1) * 3600},
         _GOOD_ARTIFACT),
    ]
    for i, (record, artifact) in enumerate(cases):
        candidate = dict(data)
        candidate["_promotions"] = {"run-1": [record]}
        store.write_text(json.dumps(candidate))
        before = store.read_bytes()
        assert gates.check_promotion(
            store, "run-1", "prod", "web", artifact, now=now,
        ) is False, f"case {i} authorized malformed promotion evidence"
        assert store.read_bytes() == before


def test_check_promotion_false_when_promotions_key_missing(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    before = store.read_bytes()
    assert gates.check_promotion(store, "run-1", "prod", "web",
                                 _GOOD_ARTIFACT) is False
    assert store.read_bytes() == before  # pure read — never migrates the store


def test_check_promotion_false_no_record_for_run_id(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="web", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_promotion(store, "run-none", "prod", "web",
                                 _GOOD_ARTIFACT) is False


# ── spec-295 Phase 1 Plan 02: check_grant_prod precondition (RED → GREEN) ────
# RED-FIRST CAPTURE (adversary MEDIUM #9): before gates.py was edited,
#   cd packages/feature-fix-swarm && python3 -m pytest lib/tests/test_gates.py -k check_grant_prod -q
# failed with:
#   AttributeError: module 'gates' has no attribute 'check_grant_prod'
# (10 tests errored). GREEN run after implementation: same command, 10 passed.

def test_check_grant_prod_no_promote_refuses_with_no_promote_evidence(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is False
    pend = gates.list_pending(store, "run-1")
    assert any("NO-PROMOTE-EVIDENCE" in p["reason"] for p in pend)


def test_check_grant_prod_artifact_mismatch_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    other_artifact = "other@sha256:" + "a" * 64
    _seed_success(store, artifact=other_artifact)
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="cp", artifact=other_artifact,
                           evidence_ids=["stg-web"])
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is False
    pend = gates.list_pending(store, "run-1")
    assert any("PROMOTE-ARTIFACT-MISMATCH" in p["reason"] for p in pend)


def test_check_grant_prod_expired_promote_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="cp", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"], ttl_hours=1.0)
    import time as _t
    now = _t.time()
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT,
                                  now=now + 3601) is False
    pend = gates.list_pending(store, "run-1")
    assert any("PROMOTE-EXPIRED" in p["reason"] for p in pend)


def test_check_grant_prod_dev_to_prod_source_refuses_as_no_promote_evidence(tmp_path) -> None:
    # adversary CRITICAL #2: a dev->prod matching-artifact promote never
    # satisfies the precondition — check_promotion's from_env guard rejects
    # it, and it must not be misclassified as a mismatch/expiry either.
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    gates.record_promotion(store, "run-1", from_env="dev", to_env="prod",
                           surface="cp", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is False
    pend = gates.list_pending(store, "run-1")
    assert any("NO-PROMOTE-EVIDENCE" in p["reason"] for p in pend)


def test_check_grant_prod_fresh_matching_promote_passes(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="cp", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is True


def test_check_grant_prod_promote_without_grant_refuses(tmp_path) -> None:
    # promote confers no authority — a fresh matching promote with NO grant
    # still refuses.
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="cp", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is False


def test_check_grant_prod_missing_artifact_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="cp", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", None) is False
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", "") is False


def test_check_grant_prod_flip_and_migrate_prefixes_route_same_precondition(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    for action, surface in (("flip:prod-blue", "blue"), ("migrate:prod-db", "db"),
                            ("deploy:prod-fleet-1", "fleet-1")):
        gates.grant_actions(store, "run-1", [action])
        assert gates.check_grant_prod(store, "run-1", action, _GOOD_ARTIFACT) is False  # no promote yet
        gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                               surface=surface, artifact=_GOOD_ARTIFACT,
                               evidence_ids=["stg-web"])
        assert gates.check_grant_prod(store, "run-1", action, _GOOD_ARTIFACT) is True


def test_check_grant_prod_manifest_staging_none_refuses_no_staging_counterpart(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.grant_actions(store, "run-1", ["deploy:prod-n8n"])
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="n8n", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    manifest = {"n8n": {"staging": "none"}}
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-n8n", _GOOD_ARTIFACT,
                                  manifest=manifest) is False
    pend = gates.list_pending(store, "run-1")
    assert any("NO-STAGING-COUNTERPART" in p["reason"] for p in pend)
    # manifest absent (None) never fabricates a refusal — same fixture passes
    store2 = tmp_path / "evidence2.json"
    _seed_success(store2)
    gates.grant_actions(store2, "run-1", ["deploy:prod-n8n"])
    gates.record_promotion(store2, "run-1", from_env="staging", to_env="prod",
                           surface="n8n", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    assert gates.check_grant_prod(store2, "run-1", "deploy:prod-n8n", _GOOD_ARTIFACT) is True


def test_load_manifest_accepts_committed_parity_yaml_shape(tmp_path) -> None:
    manifest_path = tmp_path / "parity-manifest.yaml"
    manifest_path.write_text(
        "surfaces:\n"
        "  - surface: n8n\n"
        "    staging_instance: \"none\"\n"
        "    prod_instance: \"prod-n8n\"\n"
        "    staging_artifact: \"n/a\"\n"
        "    prod_artifact: \"n/a\"\n"
        "    staging_migration_head: \"n/a\"\n"
        "    prod_migration_head: \"n/a\"\n"
    )

    assert gates._load_manifest(str(manifest_path)) == {
        "n8n": {"staging": "none"},
    }


def test_check_grant_prod_byte_identical_for_non_prod_action(tmp_path) -> None:
    # adversary MEDIUM #7: a non-prod action routes through check_grant_prod
    # with ZERO side effects — exact byte-identical store snapshot.
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["push:origin/main"])
    before = store.read_bytes()
    assert gates.check_grant_prod(store, "run-1", "push:origin/other", None) is False
    assert store.read_bytes() == before
    assert gates.check_grant_prod(store, "run-1", "push:origin/main", None) is True
    assert store.read_bytes() == before


# ── spec-295 Phase 1 Plan 02: CLI promote + check-grant --artifact (RED→GREEN) ──
# RED-FIRST CAPTURE (adversary MEDIUM #9): before gates.py was edited,
#   cd packages/feature-fix-swarm && python3 -m pytest lib/tests/test_gates.py -k "cli_promote or cli_check_grant" -q
# failed: `promote` was an unrecognized dispatch command ("unknown command:
# promote", exit 2) and `check-grant --artifact` on a prod action fell
# through to the unmodified non-prod branch (printed GRANTED with no promote
# check at all), so every subprocess returncode/stdout assertion below
# failed. GREEN run after implementation: same command, all passed.

def _seed_success_cli(env, task_id: str = "stg-web") -> None:
    import subprocess as _sp
    g = str(DISPATCH_DIR / "gates.py")
    _sp.run(["python3", g, "run-gate", task_id,
             "--artifact", _GOOD_ARTIFACT, "--", "true"],
            capture_output=True, text=True, env=env)


def test_cli_promote_records_validated_evidence(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    _seed_success_cli(env)
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "promote", "run-9", "--from", "staging", "--to", "prod",
                 "--surface", "web", "--artifact", _GOOD_ARTIFACT,
                 "--evidence", "stg-web"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "PROMOTED" in r.stdout


def test_cli_promote_collects_repeatable_evidence(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    _seed_success_cli(env, "stg-web")
    _seed_success_cli(env, "stg-web-2")
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "promote", "run-9", "--from", "staging", "--to", "prod",
                 "--surface", "web", "--artifact", _GOOD_ARTIFACT,
                 "--evidence", "stg-web", "--evidence", "stg-web-2"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "PROMOTED" in r.stdout
    data = json.loads((tmp_path / "evidence.json").read_text())
    assert data["_promotions"]["run-9"][0]["evidence_ids"] == ["stg-web", "stg-web-2"]


def test_cli_promote_rejects_malformed_artifact(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    _seed_success_cli(env)
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "promote", "run-9", "--from", "staging", "--to", "prod",
                 "--surface", "web", "--artifact", "img:latest",
                 "--evidence", "stg-web"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "PROMOTE-REJECTED" in r.stderr


def test_cli_promote_rejects_unresolved_evidence(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "promote", "run-9", "--from", "staging", "--to", "prod",
                 "--surface", "web", "--artifact", _GOOD_ARTIFACT,
                 "--evidence", "never-ran"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "PROMOTE-REJECTED" in r.stderr


def test_cli_promote_rejects_bad_ttl(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    _seed_success_cli(env)
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "promote", "run-9", "--from", "staging", "--to", "prod",
                 "--surface", "web", "--artifact", _GOOD_ARTIFACT,
                 "--evidence", "stg-web", "--ttl-hours", "not-a-number"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "PROMOTE-REJECTED" in r.stderr


def test_cli_check_grant_prod_no_promote_refuses_with_hint(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    _sp.run(["python3", g, "grant", "run-9", "--action", "deploy:prod-web"],
            capture_output=True, text=True, env=env)
    r = _sp.run(["python3", g, "check-grant", "run-9", "--action", "deploy:prod-web",
                 "--artifact", _GOOD_ARTIFACT],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "NOT-GRANTED" in r.stdout
    assert "promote" in r.stdout.lower()


def test_cli_check_grant_prod_round_trip_passes(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    _seed_success_cli(env)
    _sp.run(["python3", g, "grant", "run-9", "--action", "deploy:prod-web"],
            capture_output=True, text=True, env=env)
    _sp.run(["python3", g, "promote", "run-9", "--from", "staging", "--to", "prod",
             "--surface", "web", "--artifact", _GOOD_ARTIFACT, "--evidence", "stg-web"],
            capture_output=True, text=True, env=env)
    r = _sp.run(["python3", g, "check-grant", "run-9", "--action", "deploy:prod-web",
                 "--artifact", _GOOD_ARTIFACT],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "GRANTED" in r.stdout


def test_cli_check_grant_prod_missing_artifact_refuses(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    _seed_success_cli(env)
    _sp.run(["python3", g, "grant", "run-9", "--action", "deploy:prod-web"],
            capture_output=True, text=True, env=env)
    _sp.run(["python3", g, "promote", "run-9", "--from", "staging", "--to", "prod",
             "--surface", "web", "--artifact", _GOOD_ARTIFACT, "--evidence", "stg-web"],
            capture_output=True, text=True, env=env)
    r = _sp.run(["python3", g, "check-grant", "run-9", "--action", "deploy:prod-web"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1


def test_cli_check_grant_prod_rebuild_invalidation(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    _seed_success_cli(env)
    _sp.run(["python3", g, "grant", "run-9", "--action", "deploy:prod-web"],
            capture_output=True, text=True, env=env)
    _sp.run(["python3", g, "promote", "run-9", "--from", "staging", "--to", "prod",
             "--surface", "web", "--artifact", _GOOD_ARTIFACT, "--evidence", "stg-web"],
            capture_output=True, text=True, env=env)
    other = "myapp@sha256:" + "9" * 64
    r = _sp.run(["python3", g, "check-grant", "run-9", "--action", "deploy:prod-web",
                 "--artifact", other],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "NOT-GRANTED" in r.stdout


def test_cli_check_grant_non_prod_action_unchanged(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    _sp.run(["python3", g, "grant", "run-9", "--action", "push:origin/main"],
            capture_output=True, text=True, env=env)
    r = _sp.run(["python3", g, "check-grant", "run-9", "--action", "push:origin/main"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "GRANTED" in r.stdout


# ── spec-295 Phase 1 Plan 03 Task 1: grant reason (additive) + grant --reason ──
# RED-FIRST CAPTURE (adversary MEDIUM #9): before gates.py was edited,
#   cd packages/feature-fix-swarm && python3 -m pytest lib/tests/test_gates.py -k grant_reason -q
# failed with: TypeError: grant_actions() got an unexpected keyword argument
# 'reason' (function-level tests) and the CLI `--reason` flag was silently
# ignored — no `reason` key ever landed in the stored grant entry (CLI
# subprocess tests). GREEN run after implementation: same command, 5 passed.

def test_grant_reason_stored_sanitized(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    assert gates.grant_actions(s, "run-1", ["hotfix:prod-cp"], reason="db is down")
    data = json.loads(s.read_text())
    assert data["_autonomy"]["run-1"]["grants"]["hotfix:prod-cp"]["reason"] == "db is down"


def test_grant_no_reason_byte_identical_entry_shape(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    assert gates.grant_actions(s, "run-1", ["push:origin/main"])
    data = json.loads(s.read_text())
    entry = data["_autonomy"]["run-1"]["grants"]["push:origin/main"]
    # byte-identical to the pre-reason shape: no `reason` key at all
    assert set(entry.keys()) == {"granted_at", "expires_at", "granted_by"}


def test_grant_reason_sanitized_strips_control_chars(tmp_path) -> None:
    s = tmp_path / "evidence.json"
    gates.grant_actions(s, "run-1", ["hotfix:prod-cp"],
                        reason="db down\x1b[31mFAKE-BANNER\x1b[0m")
    data = json.loads(s.read_text())
    reason = data["_autonomy"]["run-1"]["grants"]["hotfix:prod-cp"]["reason"]
    assert "\x1b" not in reason


def test_cli_grant_reason_flag_records_reason(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "grant", "run-9", "--action", "hotfix:prod-cp",
                 "--reason", "db down"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "GRANTED" in r.stdout
    data = json.loads((tmp_path / "evidence.json").read_text())
    assert data["_autonomy"]["run-9"]["grants"]["hotfix:prod-cp"]["reason"] == "db down"


def test_cli_grant_no_reason_flag_records_no_reason(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    _sp.run(["python3", g, "grant", "run-9", "--action", "push:origin/main"],
            capture_output=True, text=True, env=env)
    data = json.loads((tmp_path / "evidence.json").read_text())
    entry = data["_autonomy"]["run-9"]["grants"]["push:origin/main"]
    assert "reason" not in entry


# ── spec-295 Phase 1 Plan 03 Task 2: hotfix:prod-* bypass (RED → GREEN) ──────
# RED-FIRST CAPTURE (adversary MEDIUM #9): before gates.py was edited,
#   cd packages/feature-fix-swarm && python3 -m pytest lib/tests/test_gates.py -k hotfix -q
# failed: a granted+reasoned hotfix:prod-* action fell through to check_grant's
# ordinary (non-prod) True path with NO bypass record written at all — the
# `_autonomy[run]["hotfix_bypasses"]` KeyError below, and the CLI printed a
# plain `GRANTED:` line containing no `EMERGENCY` token. GREEN run after
# implementation: same command, 8 passed.

def test_hotfix_granted_with_reason_bypasses_promote_and_records(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["hotfix:prod-cp"], reason="db is down")
    # no promote record present at all — the bypass is authorized anyway
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is True
    data = json.loads(store.read_text())
    bypasses = data["_autonomy"]["run-1"]["hotfix_bypasses"]
    assert len(bypasses) == 1
    assert bypasses[0]["action"] == "hotfix:prod-cp"
    assert bypasses[0]["reason"] == "db is down"


def test_hotfix_granted_without_reason_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["hotfix:prod-cp"])  # no reason
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is False
    pend = gates.list_pending(store, "run-1")
    assert any("NO-HOTFIX-GRANT" in p["reason"] for p in pend)


def test_hotfix_prod_spelling_variants_never_fall_through_plain_grant(tmp_path) -> None:
    for i, action in enumerate((
        "hotfix:PROD-cp",
        "hotfix:prod_cp",
        "hotfix:production-cp",
        "hotfix:Production_cp",
    )):
        store = tmp_path / f"variant-{i}.json"
        assert gates.grant_actions(store, "run-1", [action])
        assert gates.check_grant_prod(store, "run-1", action, None) is False
        data = json.loads(store.read_text())
        assert not data["_autonomy"]["run-1"].get("hotfix_bypasses")
        assert any("NO-HOTFIX-GRANT" in p["reason"]
                   for p in gates.list_pending(store, "run-1"))


def test_hotfix_case_variant_with_reason_is_audited(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    action = "hotfix:PROD-cp"
    assert gates.grant_actions(store, "run-1", [action], reason="db down")
    assert gates.check_grant_prod(store, "run-1", action, None) is True
    bypass = json.loads(store.read_text())["_autonomy"]["run-1"]["hotfix_bypasses"]
    assert bypass == [{
        "action": action,
        "reason": "db down",
        "recorded_at": bypass[0]["recorded_at"],
    }]


def test_hotfix_whitespace_only_reason_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    action = "hotfix:prod-cp"
    assert gates.grant_actions(store, "run-1", [action], reason=" \t ")
    assert gates.check_grant_prod(store, "run-1", action, None) is False
    entry = json.loads(store.read_text())["_autonomy"]["run-1"]["grants"][action]
    assert "reason" not in entry


def test_hotfix_no_grant_refuses_and_records_pending(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is False
    pend = gates.list_pending(store, "run-1")
    assert any("NO-HOTFIX-GRANT" in p["reason"] for p in pend)


def test_hotfix_reason_sanitized_in_bypass_record(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["hotfix:prod-cp"],
                        reason="db down\x1b[31mFAKE-BANNER\x1b[0m")
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is True
    data = json.loads(store.read_text())
    reason = data["_autonomy"]["run-1"]["hotfix_bypasses"][0]["reason"]
    assert "\x1b" not in reason


def test_hotfix_does_not_require_promote_evidence(tmp_path) -> None:
    # no --artifact given at all, no promote record anywhere — hotfix is the
    # sanctioned bypass of check_promotion, not a variant of it.
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["hotfix:prod-cp"], reason="db down")
    assert gates.check_grant_prod(store, "run-1", "hotfix:prod-cp", None) is True


def test_non_hotfix_prod_action_still_requires_promote(tmp_path) -> None:
    # Plan 02 behavior intact: an ordinary prod verb is unaffected by the
    # hotfix branch and still needs a promote record.
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp", _GOOD_ARTIFACT) is False


def test_cli_check_grant_hotfix_emergency_banner(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    _sp.run(["python3", g, "grant", "run-9", "--action", "hotfix:prod-cp",
             "--reason", "db down"],
            capture_output=True, text=True, env=env)
    r = _sp.run(["python3", g, "check-grant", "run-9", "--action", "hotfix:prod-cp"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "EMERGENCY" in r.stdout
    assert "hotfix:prod-cp" in r.stdout


def test_cli_check_grant_hotfix_without_grant_refuses(tmp_path) -> None:
    import os as _os
    import subprocess as _sp
    env = dict(_os.environ, GATES_STORE=str(tmp_path / "evidence.json"))
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "check-grant", "run-9", "--action", "hotfix:prod-cp"],
                capture_output=True, text=True, env=env)
    assert r.returncode == 1


# ── spec-295 Phase 2: preflight staging-proof kind (RED → GREEN) ────────────

def test_preflight_staging_proof_empty_ledger_fails(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    res = gates.preflight_check(
        [{"kind": "staging-proof", "name": "web", "artifact": _GOOD_ARTIFACT}],
        store=store, run_id="run-1")
    assert res["pass"] is False
    assert res["results"][0]["ok"] is False


def test_preflight_staging_proof_ok_after_promote(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_success(store)
    gates.record_promotion(store, "run-1", from_env="staging", to_env="prod",
                           surface="web", artifact=_GOOD_ARTIFACT,
                           evidence_ids=["stg-web"])
    res = gates.preflight_check(
        [{"kind": "staging-proof", "name": "web", "artifact": _GOOD_ARTIFACT}],
        store=store, run_id="run-1")
    assert res["pass"] is True
    assert res["results"][0]["ok"] is True


def test_preflight_staging_proof_no_store_fails_closed(tmp_path) -> None:
    res = gates.preflight_check(
        [{"kind": "staging-proof", "name": "web", "artifact": _GOOD_ARTIFACT}])
    assert res["pass"] is False
    assert res["results"][0]["ok"] is False


def test_preflight_unknown_kind_fails_closed(tmp_path) -> None:
    res = gates.preflight_check([{"kind": "bogus", "name": "whatever"}])
    assert res["pass"] is False
    assert res["results"][0]["ok"] is False


# ── spec-295 review-gate hardening (RED → GREEN) ───────────────────────────

def test_record_promotion_rejects_caller_fabricated_gate(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_gate_evidence(store, "stg-web", exit_code=0, cmd="pytest -q")
    assert gates.record_promotion(
        store, "run-1", from_env="staging", to_env="prod",
        surface="web", artifact=_GOOD_ARTIFACT, evidence_ids=["stg-web"],
    ) is False


def test_record_promotion_requires_runner_evidence_bound_to_artifact(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    other_artifact = "myapp@sha256:" + "e" * 64
    assert gates.run_gate(
        store, "stg-web", ["python3", "-c", "raise SystemExit(0)"],
        artifact=_GOOD_ARTIFACT,
    ) == 0
    assert gates.record_promotion(
        store, "run-1", from_env="staging", to_env="prod",
        surface="web", artifact=other_artifact, evidence_ids=["stg-web"],
    ) is False
    assert gates.record_promotion(
        store, "run-1", from_env="staging", to_env="prod",
        surface="web", artifact=_GOOD_ARTIFACT, evidence_ids=["stg-web"],
    ) is True


def test_prod_action_spelling_variants_never_fall_through_to_plain_grant(tmp_path) -> None:
    for action in (
        "deploy:prod", "flip:prod_api", "migrate:production-db",
        "deploy:PROD-web", "flip:Prod_api", "migrate:Production-db",
    ):
        store = tmp_path / f"{action.replace(':', '-')}.json"
        assert gates.grant_actions(store, "run-1", [action])
        assert gates.check_grant_prod(store, "run-1", action, _GOOD_ARTIFACT) is False
        assert gates.list_pending(store, "run-1")


def test_cli_promote_trailing_evidence_flag_returns_typed_rejection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GATES_STORE", str(tmp_path / "evidence.json"))
    rc = gates.main([
        "promote", "run-1", "--from", "staging", "--to", "prod",
        "--surface", "web", "--artifact", _GOOD_ARTIFACT, "--evidence",
    ])
    assert rc == 1


# ── spec-008 Phase 3 (REQ-301): record_canary_evidence recorder validation ───
# Trust boundary (wall 087faa76): the recorder validates SHAPE, not caller
# identity — the trusted wrapper (canary-gate.sh) is the sole LEGITIMATE
# producer; integrity is guarded by the G2 tamper scan + store perms.

_COMMIT_ARTIFACT = "ab" * 20  # bare 40-hex commit sha (F5 — the FFS binding target)
_ISO_CREATED = "2026-06-13T08:43:44.814Z"
_ISO_ENDED = "2026-06-13T08:48:02.991Z"


def test_canary_recorder_appends_verbatim_schema_row(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_canary_evidence(store, "run-1", _COMMIT_ARTIFACT, True,
                                 _ISO_CREATED, _ISO_ENDED)
    rows = json.loads(store.read_text())["canary"]
    assert len(rows) == 1
    rec = rows[0]
    assert set(rec) == {"run_id", "sha", "pass", "created_at", "ended_at", "ts"}
    assert rec["run_id"] == "run-1"
    assert rec["sha"] == _COMMIT_ARTIFACT
    assert rec["pass"] is True
    assert rec["created_at"] == _ISO_CREATED
    assert rec["ended_at"] == _ISO_ENDED
    assert isinstance(rec["ts"], float)


def test_canary_recorder_rejects_non_40_hex_sha(tmp_path) -> None:
    import pytest
    store = tmp_path / "evidence.json"
    for bad in ("abc", "Z" * 40, "ab" * 32, "", None,
                "myapp@sha256:" + "f" * 64):  # digest refs are NOT canary identity
        with pytest.raises(ValueError, match="INVALID-CANARY-SHA"):
            gates.record_canary_evidence(store, "run-1", bad, True,
                                         _ISO_CREATED, _ISO_ENDED)
    assert not store.exists()


def test_canary_recorder_rejects_empty_or_missing_timestamps(tmp_path) -> None:
    import pytest
    store = tmp_path / "evidence.json"
    for created, ended in (("", _ISO_ENDED), (_ISO_CREATED, ""), (None, _ISO_ENDED),
                           (_ISO_CREATED, None), ("   ", _ISO_ENDED),
                           ("x" * 65, _ISO_ENDED), ("bad\x00ts", _ISO_ENDED)):
        with pytest.raises(ValueError, match="INVALID-CANARY-TIMESTAMP"):
            gates.record_canary_evidence(store, "run-1", _COMMIT_ARTIFACT, True,
                                         created, ended)
    assert not store.exists()


def test_canary_recorder_rejects_malformed_pass_flag(tmp_path) -> None:
    import pytest
    store = tmp_path / "evidence.json"
    for bad in (1, 0, "true", "false", None):
        with pytest.raises(ValueError, match="INVALID-CANARY-PASS"):
            gates.record_canary_evidence(store, "run-1", _COMMIT_ARTIFACT, bad,
                                         _ISO_CREATED, _ISO_ENDED)
    assert not store.exists()


def test_canary_recorder_rejects_invalid_run_id(tmp_path) -> None:
    import pytest
    store = tmp_path / "evidence.json"
    for bad in ("", None, "x" * 129, "bad\x00run"):
        with pytest.raises(ValueError, match="INVALID-CANARY-RUN-ID"):
            gates.record_canary_evidence(store, bad, _COMMIT_ARTIFACT, True,
                                         _ISO_CREATED, _ISO_ENDED)
    assert not store.exists()


def test_canary_recorder_refuses_namespace_shape_conflict(tmp_path) -> None:
    import pytest
    store = tmp_path / "evidence.json"
    store.write_text(json.dumps({"canary": {"not": "a list"}}))
    with pytest.raises(ValueError, match="CANARY-SCHEMA-CONFLICT"):
        gates.record_canary_evidence(store, "run-1", _COMMIT_ARTIFACT, True,
                                     _ISO_CREATED, _ISO_ENDED)
    assert json.loads(store.read_text())["canary"] == {"not": "a list"}


def test_cli_canary_evidence_records_and_rejects_typed(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GATES_STORE", str(tmp_path / "evidence.json"))
    rc = gates.main(["canary-evidence", "--run-id", "run-1",
                     "--sha", _COMMIT_ARTIFACT, "--pass", "true",
                     "--created-at", _ISO_CREATED, "--ended-at", _ISO_ENDED])
    assert rc == 0
    rows = json.loads((tmp_path / "evidence.json").read_text())["canary"]
    assert rows[0]["sha"] == _COMMIT_ARTIFACT and rows[0]["pass"] is True
    rc = gates.main(["canary-evidence", "--run-id", "run-1",
                     "--sha", "not-a-sha", "--pass", "true",
                     "--created-at", _ISO_CREATED, "--ended-at", _ISO_ENDED])
    assert rc == 2
    assert "CANARY-EVIDENCE-REJECTED" in capsys.readouterr().err


# ── spec-008 Phase 3 (REQ-301, AC-004): check_promotion canary sha binding ───
# Canary-surface classifier (pinned decision 1): binding is STORE-SCOPED — a
# non-empty canary namespace makes every check_promotion candidate
# canary-bound (typed pass record with exact-matching sha required; sha-less
# or untyped entries trigger binding but never satisfy it); an empty
# namespace leaves promotion behavior unchanged.

_OTHER_COMMIT = "cd" * 20


def _seed_promotion(store, run_id: str = "run-1", surface: str = "cp",
                    artifact: str = _COMMIT_ARTIFACT, **kwargs) -> None:
    _seed_success(store, artifact=artifact)
    assert gates.record_promotion(store, run_id, from_env="staging",
                                  to_env="prod", surface=surface,
                                  artifact=artifact, evidence_ids=["stg-web"],
                                  **kwargs) is True


def _inject_canary(store, rows) -> None:
    data = json.loads(store.read_text()) if store.exists() else {}
    data["canary"] = rows
    store.write_text(json.dumps(data))


def test_ac004_typed_match_allows(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    gates.record_canary_evidence(store, "run-1", _COMMIT_ARTIFACT, True,
                                 _ISO_CREATED, _ISO_ENDED)
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is True


def test_ac004_sha_mismatch_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    gates.record_canary_evidence(store, "run-1", _OTHER_COMMIT, True,
                                 _ISO_CREATED, _ISO_ENDED)
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is False


def test_ac004_missing_sha_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    _inject_canary(store, [
        {"run_id": "run-1", "pass": True, "created_at": _ISO_CREATED,
         "ended_at": _ISO_ENDED, "ts": 1.0},  # typed kind, no sha at all
        {"run_id": "run-1", "sha": "shortsha", "pass": True,
         "created_at": _ISO_CREATED, "ended_at": _ISO_ENDED, "ts": 1.0},
    ])
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is False


def test_ac004_untyped_legacy_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    _inject_canary(store, [{"marker": "legacy-canary"}, "opaque-string", 7])
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is False
    # a tampered non-list namespace also refuses — never raises (pure read)
    data = json.loads(store.read_text())
    data["canary"] = {"not": "a list"}
    store.write_text(json.dumps(data))
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is False


def test_ac004_empty_namespace_unaffected(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is True  # absent namespace
    _inject_canary(store, [])
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is True  # explicitly empty


def test_ac004_freshness_independent(tmp_path) -> None:
    import time as _t
    # fresh promotion + mismatched canary → refused (binding refuses)
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    gates.record_canary_evidence(store, "run-1", _OTHER_COMMIT, True,
                                 _ISO_CREATED, _ISO_ENDED)
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is False
    # expired promotion + exact-matching canary → refused (expires_at intact)
    store2 = tmp_path / "evidence2.json"
    _seed_promotion(store2, ttl_hours=1.0)
    gates.record_canary_evidence(store2, "run-1", _COMMIT_ARTIFACT, True,
                                 _ISO_CREATED, _ISO_ENDED)
    assert gates.check_promotion(store2, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT, now=_t.time() + 3601) is False


def test_ac004_failed_canary_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    gates.record_canary_evidence(store, "run-1", _COMMIT_ARTIFACT, False,
                                 _ISO_CREATED, _ISO_ENDED)
    assert gates.check_promotion(store, "run-1", "prod", "cp",
                                 _COMMIT_ARTIFACT) is False


def test_ac004_miss_reason_canary_evidence_required(tmp_path) -> None:
    # fresh, fully-matching promotion but only untyped canary entries →
    # check_grant_prod names the refusal CANARY-EVIDENCE-REQUIRED
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    _inject_canary(store, [{"marker": "legacy-canary"}])
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT) is False
    pend = gates.list_pending(store, "run-1")
    assert any("CANARY-EVIDENCE-REQUIRED" in p["reason"] for p in pend)


def test_ac004_miss_reason_canary_sha_mismatch(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _seed_promotion(store)
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    gates.record_canary_evidence(store, "run-1", _OTHER_COMMIT, True,
                                 _ISO_CREATED, _ISO_ENDED)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT) is False
    pend = gates.list_pending(store, "run-1")
    assert any("CANARY-SHA-MISMATCH" in p["reason"] for p in pend)


def test_ac004_miss_reason_phase1_ordering_preserved(tmp_path) -> None:
    # canary evidence present but NO promote record at all → the phase-1
    # NO-PROMOTE-EVIDENCE reason still fires (AC-001 ordering intact) …
    store = tmp_path / "evidence.json"
    gates.grant_actions(store, "run-1", ["deploy:prod-cp"])
    gates.record_canary_evidence(store, "run-1", _COMMIT_ARTIFACT, True,
                                 _ISO_CREATED, _ISO_ENDED)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT) is False
    pend = gates.list_pending(store, "run-1")
    assert any("NO-PROMOTE-EVIDENCE" in p["reason"] for p in pend)
    assert not any("CANARY" in p["reason"] for p in pend)
    # … and an EXPIRED promote with a matching canary still classifies as
    # PROMOTE-EXPIRED, never a canary reason.
    import time as _t
    store2 = tmp_path / "evidence2.json"
    _seed_promotion(store2, ttl_hours=1.0)
    gates.grant_actions(store2, "run-1", ["deploy:prod-cp"])
    gates.record_canary_evidence(store2, "run-1", _COMMIT_ARTIFACT, True,
                                 _ISO_CREATED, _ISO_ENDED)
    assert gates.check_grant_prod(store2, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT, now=_t.time() + 3601) is False
    pend = gates.list_pending(store2, "run-1")
    assert any("PROMOTE-EXPIRED" in p["reason"] for p in pend)
    assert not any("CANARY" in p["reason"] for p in pend)


# ── spec-008 Phase 3 (REQ-302, AC-005): rollback_dryrun schema + gate ────────
# Synthetic surfaces + synthetic manifest dicts only: no real FFS surface
# declares rollback and no config/ directory exists, so production behavior
# is a structural no-op (locked row 11 — fallback-rehearsal.sh is NOT wired).
# "Fresh same-run" = the run_id being checked, i.e. check_grant_prod's OWN
# authoritative run_id parameter (wall 7531f885) — never an env default.

_ROLLBACK_CMD = "deploy rollback cp"
_ROLLBACK_MANIFEST = {"cp": {"staging": "stg-cp", "rollback": _ROLLBACK_CMD}}


def _grant_and_promote(store, run_id: str = "run-1") -> None:
    _seed_promotion(store, run_id=run_id)
    assert gates.grant_actions(store, run_id, ["deploy:prod-cp"]) is True


def test_ac005_declared_fresh_pass_allows(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _grant_and_promote(store)
    gates.record_rollback_dryrun(store, "run-1", "cp", _ROLLBACK_CMD, 0,
                                 _COMMIT_ARTIFACT)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT,
                                  manifest=_ROLLBACK_MANIFEST) is True


def test_ac005_missing_record_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _grant_and_promote(store)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT,
                                  manifest=_ROLLBACK_MANIFEST) is False
    pend = gates.list_pending(store, "run-1")
    assert any("ROLLBACK-DRYRUN-REQUIRED" in p["reason"] and "cp" in p["reason"]
               for p in pend)


def test_ac005_other_surface_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _grant_and_promote(store)
    gates.record_rollback_dryrun(store, "run-1", "db", _ROLLBACK_CMD, 0,
                                 _COMMIT_ARTIFACT)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT,
                                  manifest=_ROLLBACK_MANIFEST) is False


def test_ac005_other_run_refuses(tmp_path) -> None:
    # a stale record from another run_id never satisfies — the same-run
    # comparison binds check_grant_prod's own run_id parameter
    store = tmp_path / "evidence.json"
    _grant_and_promote(store)
    gates.record_rollback_dryrun(store, "run-2", "cp", _ROLLBACK_CMD, 0,
                                 _COMMIT_ARTIFACT)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT,
                                  manifest=_ROLLBACK_MANIFEST) is False


def test_ac005_failed_dryrun_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _grant_and_promote(store)
    gates.record_rollback_dryrun(store, "run-1", "cp", _ROLLBACK_CMD, 1,
                                 _COMMIT_ARTIFACT)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT,
                                  manifest=_ROLLBACK_MANIFEST) is False


def test_ac005_command_mismatch_refuses(tmp_path) -> None:
    # wall 4e3862e5: command must string-equal the manifest-declared command
    store = tmp_path / "evidence.json"
    _grant_and_promote(store)
    gates.record_rollback_dryrun(store, "run-1", "cp", "some other command", 0,
                                 _COMMIT_ARTIFACT)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT,
                                  manifest=_ROLLBACK_MANIFEST) is False


def test_ac005_sha_mismatch_refuses(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    _grant_and_promote(store)
    gates.record_rollback_dryrun(store, "run-1", "cp", _ROLLBACK_CMD, 0,
                                 _OTHER_COMMIT)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT,
                                  manifest=_ROLLBACK_MANIFEST) is False


def test_ac005_undeclared_noop(tmp_path) -> None:
    # no rollback key in the manifest row (every real FFS surface) and the
    # no-manifest path: the gate is a no-op by construction
    store = tmp_path / "evidence.json"
    _grant_and_promote(store)
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT,
                                  manifest={"cp": {"staging": "stg-cp"}}) is True
    assert gates.check_grant_prod(store, "run-1", "deploy:prod-cp",
                                  _COMMIT_ARTIFACT, manifest=None) is True


def test_ac005_recorder_appends_verbatim_schema_row(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    gates.record_rollback_dryrun(store, "run-1", "cp", _ROLLBACK_CMD, 0,
                                 _COMMIT_ARTIFACT)
    rows = json.loads(store.read_text())["rollback_dryrun"]
    assert len(rows) == 1
    rec = rows[0]
    assert set(rec) == {"run_id", "surface", "command", "exit_code",
                        "artifact_sha", "ts"}
    assert rec["run_id"] == "run-1" and rec["surface"] == "cp"
    assert rec["command"] == _ROLLBACK_CMD and rec["exit_code"] == 0
    assert rec["artifact_sha"] == _COMMIT_ARTIFACT
    assert isinstance(rec["ts"], float)


def test_ac005_recorder_rejects_typed(tmp_path) -> None:
    import pytest
    store = tmp_path / "evidence.json"
    with pytest.raises(ValueError, match="INVALID-ROLLBACK-RUN-ID"):
        gates.record_rollback_dryrun(store, "", "cp", _ROLLBACK_CMD, 0,
                                     _COMMIT_ARTIFACT)
    with pytest.raises(ValueError, match="INVALID-ROLLBACK-SURFACE"):
        gates.record_rollback_dryrun(store, "run-1", "  ", _ROLLBACK_CMD, 0,
                                     _COMMIT_ARTIFACT)
    with pytest.raises(ValueError, match="INVALID-ROLLBACK-COMMAND"):
        gates.record_rollback_dryrun(store, "run-1", "cp", "", 0,
                                     _COMMIT_ARTIFACT)
    for bad_exit in ("0", 0.5, None, True):
        with pytest.raises(ValueError, match="INVALID-ROLLBACK-EXIT-CODE"):
            gates.record_rollback_dryrun(store, "run-1", "cp", _ROLLBACK_CMD,
                                         bad_exit, _COMMIT_ARTIFACT)
    with pytest.raises(ValueError, match="INVALID-ROLLBACK-ARTIFACT"):
        gates.record_rollback_dryrun(store, "run-1", "cp", _ROLLBACK_CMD, 0,
                                     "img:latest")
    assert not store.exists()


def test_ac005_recorder_refuses_namespace_shape_conflict(tmp_path) -> None:
    import pytest
    store = tmp_path / "evidence.json"
    store.write_text(json.dumps({"rollback_dryrun": {"not": "a list"}}))
    with pytest.raises(ValueError, match="ROLLBACK-DRYRUN-SCHEMA-CONFLICT"):
        gates.record_rollback_dryrun(store, "run-1", "cp", _ROLLBACK_CMD, 0,
                                     _COMMIT_ARTIFACT)


def test_cli_rollback_dryrun_records_and_rejects_typed(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GATES_STORE", str(tmp_path / "evidence.json"))
    rc = gates.main(["rollback-dryrun", "--run-id", "run-1", "--surface", "cp",
                     "--command", _ROLLBACK_CMD, "--exit-code", "0",
                     "--artifact-sha", _COMMIT_ARTIFACT])
    assert rc == 0
    rows = json.loads((tmp_path / "evidence.json").read_text())["rollback_dryrun"]
    assert rows[0]["exit_code"] == 0 and rows[0]["command"] == _ROLLBACK_CMD
    rc = gates.main(["rollback-dryrun", "--run-id", "run-1", "--surface", "cp",
                     "--command", _ROLLBACK_CMD, "--exit-code", "zero",
                     "--artifact-sha", _COMMIT_ARTIFACT])
    assert rc == 2
    assert "ROLLBACK-DRYRUN-REJECTED" in capsys.readouterr().err


def test_ac005_normalize_manifest_preserves_optional_rollback(tmp_path) -> None:
    import pytest
    normalized = gates._normalize_manifest({"surfaces": [
        {"surface": "cp", "staging_instance": "stg-cp",
         "rollback": _ROLLBACK_CMD},
        {"surface": "db", "staging_instance": "stg-db"},
    ]})
    assert normalized["cp"] == {"staging": "stg-cp", "rollback": _ROLLBACK_CMD}
    assert normalized["db"] == {"staging": "stg-db"}  # no rollback key at all
    for bad in (7, "", "   "):
        with pytest.raises(ValueError):
            gates._normalize_manifest({"surfaces": [
                {"surface": "cp", "staging_instance": "stg-cp",
                 "rollback": bad}]})


# ── spec-008 Phase 4: durable loop-cap event producer (REQ-701 OQ-1) ────────

def test_loop_cap_appends_durable_typed_event(tmp_path, monkeypatch, capsys) -> None:
    """The cap branch appends one {kind, run_id, loop, round, ts} row to the
    top-level events list so the digest has a durable producer; sub-cap
    rounds append nothing."""
    store = tmp_path / "evidence.json"
    monkeypatch.setenv("GATES_STORE", str(store))
    assert gates.main(["loop-round", "spec-008", "wall:p1", "--max", "1"]) == 0
    assert json.loads(store.read_text()).get("events", []) == []
    assert gates.main(["loop-round", "spec-008", "wall:p1", "--max", "1"]) == 1
    assert "LOOP-CAP:" in capsys.readouterr().out
    events = json.loads(store.read_text())["events"]
    assert len(events) == 1
    ev = events[0]
    assert set(ev) == {"kind", "run_id", "loop", "round", "ts"}
    assert ev["kind"] == "loop-cap"
    assert ev["run_id"] == "spec-008"
    assert ev["loop"] == "wall:p1"
    assert ev["round"] == 2
    assert isinstance(ev["ts"], float)


def test_loop_cap_event_write_failure_is_fail_soft(tmp_path, monkeypatch, capsys) -> None:
    """Decision 1: event-write failure warns on stderr but the cap STILL
    returns 1 with its LOOP-CAP line intact — observability never gates
    the cap. The rc-3 store-unusable path is untouched (counter save fails
    first there)."""
    store = tmp_path / "evidence.json"
    monkeypatch.setenv("GATES_STORE", str(store))
    assert gates.main(["loop-round", "spec-008", "wall:p1", "--max", "1"]) == 0
    real_save = gates._save_store
    calls = {"n": 0}

    def flaky(path, data):
        calls["n"] += 1
        if calls["n"] == 2:  # call 1 = counter save, call 2 = event append
            raise OSError("disk full")
        real_save(path, data)

    monkeypatch.setattr(gates, "_save_store", flaky)
    assert gates.main(["loop-round", "spec-008", "wall:p1", "--max", "1"]) == 1
    captured = capsys.readouterr()
    assert "LOOP-CAP:" in captured.out
    assert "LOOP-CAP-EVENT-UNRECORDED" in captured.err
    assert "events" not in json.loads(store.read_text())


# ── spec-007 Phase 1: env registry resolution ────────────────────────────────
# PD-2 taxonomy (2c72f448): IMPLICIT default-filename resolution parses HEAD
# bytes with HEAD as SOLE authority (index and working tree never enter the
# verdict); CALLER-SUPPLIED paths parse WORKING-TREE bytes. These cases carry
# the implicit half; the caller-supplied half lands in the Task-2 section.
# Isolation: GATES_STORE pinned under tmp_path; FFS_ENV_REGISTRY* deleted.
# Verdicts asserted via the pending record — stdout reason text is 01-02's.

_ENV_REGISTRY_WEB = (
    "# schema: ffs.environments/v1\n"
    "surfaces:\n"
    "  - surface: web\n"
    "    staging_instance: none\n"
)


def _env_registry_env(store: Path) -> dict:
    import os as _os
    env = dict(_os.environ, GATES_STORE=str(store))
    env.pop("FFS_ENV_REGISTRY", None)
    env.pop("FFS_ENV_REGISTRY_REQUIRED", None)
    return env


def _init_registry_repo(tmp_path: Path, text: str = _ENV_REGISTRY_WEB, *,
                        add: bool = True, commit: bool = True,
                        rel: str = "config/environments.yaml") -> Path:
    """Synthetic MAIN checkout holding a registry file at `rel` (pattern:
    test_store_path_resolves_worktree_to_main_checkout)."""
    import subprocess as _sp
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    _sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    (repo / rel).write_text(text)
    if add:
        _sp.run(["git", "add", rel], cwd=repo, check=True)
    if commit:
        _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "registry"], cwd=repo, check=True)
    return repo


def _seed_prod_ok(env: dict, repo: Path, run_id: str = "run-1",
                  surface: str = "web") -> None:
    """Grant + promote seeded so a NO-STAGING-COUNTERPART verdict cannot come
    from a missing grant or missing promote evidence instead."""
    import subprocess as _sp
    g = str(DISPATCH_DIR / "gates.py")
    r = _sp.run(["python3", g, "run-gate", "stg-web",
                 "--artifact", _GOOD_ARTIFACT, "--", "true"],
                capture_output=True, text=True, env=env, cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    r = _sp.run(["python3", g, "promote", run_id, "--from", "staging",
                 "--to", "prod", "--surface", surface,
                 "--artifact", _GOOD_ARTIFACT, "--evidence", "stg-web"],
                capture_output=True, text=True, env=env, cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    r = _sp.run(["python3", g, "grant", run_id,
                 "--action", f"deploy:prod-{surface}"],
                capture_output=True, text=True, env=env, cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr


def _check_grant_prod_cli(env: dict, repo: Path, run_id: str = "run-1",
                          action: str = "deploy:prod-web",
                          extra: list[str] | None = None):
    import subprocess as _sp
    g = str(DISPATCH_DIR / "gates.py")
    return _sp.run(["python3", g, "check-grant", run_id, "--action", action,
                    "--artifact", _GOOD_ARTIFACT] + (extra or []),
                   capture_output=True, text=True, env=env, cwd=repo)


def _pending_reasons(store: Path, run_id: str = "run-1") -> list[str]:
    return [p["reason"] for p in gates.list_pending(store, run_id)]


def _advisory_count(stderr: str, prefix: str) -> int:
    return sum(1 for ln in stderr.splitlines() if ln.startswith(prefix))


def test_env_registry_default_filename_resolution_refuses_in_tracked_repo(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    env = _env_registry_env(store)
    repo = _init_registry_repo(tmp_path)
    _seed_prod_ok(env, repo)
    r = _check_grant_prod_cli(env, repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert any("NO-STAGING-COUNTERPART" in x for x in _pending_reasons(store))


def test_env_registry_untracked_file_is_not_authoritative(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    env = _env_registry_env(store)
    repo = _init_registry_repo(tmp_path, add=False, commit=False)
    _seed_prod_ok(env, repo)
    r = _check_grant_prod_cli(env, repo)
    assert r.returncode == 0 and "GRANTED" in r.stdout, r.stdout + r.stderr
    assert _advisory_count(r.stderr, "ENV-REGISTRY-ABSENT:") == 1
    absent = next(ln for ln in r.stderr.splitlines()
                  if ln.startswith("ENV-REGISTRY-ABSENT:"))
    assert "config/environments.yaml" in absent and "git commit" in absent


def test_env_registry_staged_but_uncommitted_is_absent(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    env = _env_registry_env(store)
    repo = _init_registry_repo(tmp_path, add=True, commit=False)
    _seed_prod_ok(env, repo)
    r = _check_grant_prod_cli(env, repo)
    assert r.returncode == 0 and "GRANTED" in r.stdout, r.stdout + r.stderr
    assert _advisory_count(r.stderr, "ENV-REGISTRY-ABSENT:") == 1


def test_env_registry_staged_deletion_does_not_disable_gate(tmp_path) -> None:
    import subprocess as _sp
    store = tmp_path / "evidence.json"
    env = _env_registry_env(store)
    repo = _init_registry_repo(tmp_path)
    _sp.run(["git", "rm", "-q", "--cached", "config/environments.yaml"],
            cwd=repo, check=True)
    (repo / "config/environments.yaml").unlink()
    _seed_prod_ok(env, repo)
    r = _check_grant_prod_cli(env, repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert any("NO-STAGING-COUNTERPART" in x for x in _pending_reasons(store))
    assert _advisory_count(r.stderr, "ENV-REGISTRY-ABSENT:") == 0
    assert _advisory_count(r.stderr, "ENV-REGISTRY-DIRTY:") == 0


def test_env_registry_working_tree_delete_does_not_disable_gate(tmp_path) -> None:
    store = tmp_path / "evidence.json"
    env = _env_registry_env(store)
    repo = _init_registry_repo(tmp_path)
    (repo / "config/environments.yaml").unlink()
    _seed_prod_ok(env, repo)
    r = _check_grant_prod_cli(env, repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert any("NO-STAGING-COUNTERPART" in x for x in _pending_reasons(store))
    assert _advisory_count(r.stderr, "ENV-REGISTRY-ABSENT:") == 0
    assert _advisory_count(r.stderr, "ENV-REGISTRY-DIRTY:") == 0


def test_env_registry_dirty_working_copy_is_inert(tmp_path) -> None:
    import subprocess as _sp
    dirty_text = _ENV_REGISTRY_WEB.replace(
        "staging_instance: none", "staging_instance: staging-web")
    for variant in ("unstaged", "staged"):
        base = tmp_path / variant
        base.mkdir()
        store = base / "evidence.json"
        env = _env_registry_env(store)
        repo = _init_registry_repo(base)
        (repo / "config/environments.yaml").write_text(dirty_text)
        if variant == "staged":
            _sp.run(["git", "add", "config/environments.yaml"],
                    cwd=repo, check=True)
        _seed_prod_ok(env, repo)
        r = _check_grant_prod_cli(env, repo)
        # verdict comes from HEAD bytes (staging none), not the dirty copy
        assert r.returncode == 1, variant + ": " + r.stdout + r.stderr
        assert any("NO-STAGING-COUNTERPART" in x for x in _pending_reasons(store))
        assert _advisory_count(r.stderr, "ENV-REGISTRY-DIRTY:") == 1, r.stderr
    # clean-tree run emits zero advisory lines
    clean = tmp_path / "clean"
    clean.mkdir()
    store = clean / "evidence.json"
    env = _env_registry_env(store)
    repo = _init_registry_repo(clean)
    _seed_prod_ok(env, repo)
    r = _check_grant_prod_cli(env, repo)
    assert r.returncode == 1
    assert _advisory_count(r.stderr, "ENV-REGISTRY-DIRTY:") == 0
    assert _advisory_count(r.stderr, "ENV-REGISTRY-ABSENT:") == 0


def test_env_registry_unreadable_working_copy_still_resolves(tmp_path) -> None:
    # PD-2: HEAD is the sole authority for the implicit step — readability of
    # the working-tree copy is irrelevant (discriminating twin: a chmod-000
    # CALLER-SUPPLIED path REJECTS, Task-2 section).
    store = tmp_path / "evidence.json"
    env = _env_registry_env(store)
    repo = _init_registry_repo(tmp_path)
    (repo / "config/environments.yaml").chmod(0o000)
    try:
        _seed_prod_ok(env, repo)
        r = _check_grant_prod_cli(env, repo)
        assert r.returncode == 1, r.stdout + r.stderr
        assert any("NO-STAGING-COUNTERPART" in x
                   for x in _pending_reasons(store))
    finally:
        (repo / "config/environments.yaml").chmod(0o644)


def test_env_registry_live_ffs_registry_loads() -> None:
    # FFS's own committed registry parses to exactly one release row.
    live = DISPATCH_DIR.parent / "config" / "environments.yaml"
    assert gates._load_manifest(str(live)) == {"release": {"staging": "none"}}
