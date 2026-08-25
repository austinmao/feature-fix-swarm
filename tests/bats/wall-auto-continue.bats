#!/usr/bin/env bats

# _gsd_run_wall_gate: the --autonomous bounded rc-3 auto-continue seam in
# gsd-run.sh. Exercised in isolation: the function is extracted from the
# script (no sourcing — gsd-run.sh has side effects at load), the wall lever
# is a scripted stub, gates.py is the real one against a scratch store.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  WORK="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$WORK/.planning/phases/04-foo"
  export GATES_STORE="$BATS_TEST_TMPDIR/evidence.json"
  export GSD_RUN_ID="testrun"
  export REPO_ROOT="$WORK"
  GATES="$ROOT/lib/gates.py"
  # the gate resolves gates.py repo-first; give the scratch repo a real copy
  mkdir -p "$WORK/lib"
  cp "$GATES" "$WORK/lib/gates.py"
  PHASE_DIR="$WORK/.planning/phases/04-foo"
  printf '%s\n' plan > "$PHASE_DIR/01-PLAN.md"
  # scripted wall stub: emits one rc per call, from a queue file
  export WALL_RC_QUEUE="$BATS_TEST_TMPDIR/wall-rcs"
  export WALL_CALL_LOG="$BATS_TEST_TMPDIR/wall-calls"
  : > "$WALL_CALL_LOG"
  PLAN_WALL_LEVER="$BATS_TEST_TMPDIR/wall-stub.sh"
  cat > "$PLAN_WALL_LEVER" <<'STUB'
#!/usr/bin/env bash
echo "called $1" >> "$WALL_CALL_LOG"
rc="$(head -n1 "$WALL_RC_QUEUE")"
sed -i.bak '1d' "$WALL_RC_QUEUE" 2>/dev/null || true
exit "${rc:-0}"
STUB
  chmod +x "$PLAN_WALL_LEVER"
  export PLAN_WALL_LEVER
  # extract the function from the shipped script
  GATE_SRC="$BATS_TEST_TMPDIR/gate-fn.sh"
  sed -n '/^_gsd_run_wall_gate() {/,/^}/p' "$ROOT/scripts/gsd/gsd-run.sh" > "$GATE_SRC"
  [ -s "$GATE_SRC" ]
  # a truncated extraction (e.g. a future col-0 "}" inside the body) must
  # fail loudly here, not silently test a partial function
  bash -n "$GATE_SRC"
  [ "$(tail -n1 "$GATE_SRC")" = "}" ]
  grep -q 'return "$rc"' "$GATE_SRC"
}

run_gate() {
  bash -c "source '$GATE_SRC'; _gsd_run_wall_gate '$PHASE_DIR'"
}

@test "wall pass (rc 0) flows through untouched" {
  printf '0\n' > "$WALL_RC_QUEUE"
  run -0 run_gate
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 1 ]
  [[ "$output" != *"WALL-AUTO-CONTINUE"* ]]
}

@test "non-rc3 failure (rc 1) flows through untouched" {
  printf '1\n' > "$WALL_RC_QUEUE"
  run -1 run_gate
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 1 ]
  [[ "$output" != *"WALL-AUTO-CONTINUE"* ]]
}

@test "rc 3 with unresolved HIGH finding: quarantine stands, no reset" {
  python3 "$GATES" findings-queue add "f.py" "bad" --severity HIGH \
    --run-id "$GSD_RUN_ID" --source wall --plan ".planning/phases/04-foo/01-PLAN.md"
  printf '3\n' > "$WALL_RC_QUEUE"
  run -3 run_gate
  [[ "$output" == *"WALL-AUTO-CONTINUE skipped"* ]]
  [[ "$output" == *"unresolved HIGH/CRITICAL"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 1 ]
}

@test "rc 3, zero unresolved, no grant: quarantine stands" {
  printf '3\n' > "$WALL_RC_QUEUE"
  run -3 run_gate
  [[ "$output" == *"skipped (no wall-reset:04-foo grant"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 1 ]
}

@test "rc 3, zero unresolved, granted: reset + one re-run, pass clears" {
  python3 "$GATES" grant "$GSD_RUN_ID" --action wall-reset:04-foo --rollback "n/a"
  printf '3\n0\n' > "$WALL_RC_QUEUE"
  run -0 run_gate
  [[ "$output" == *"WALL-AUTO-CONTINUE phase=04-foo"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 2 ]
}

@test "budget is spent regardless: second rc 3 in same run quarantines" {
  python3 "$GATES" grant "$GSD_RUN_ID" --action wall-reset:04-foo --rollback "n/a"
  printf '3\n3\n' > "$WALL_RC_QUEUE"
  run -3 run_gate
  [[ "$output" == *"WALL-AUTO-CONTINUE exhausted (re-run rc=3)"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 2 ]
  # a later rc-3 (e.g. plan touched again) finds the budget gone
  : > "$WALL_CALL_LOG"
  printf '3\n' > "$WALL_RC_QUEUE"
  run -3 run_gate
  [[ "$output" == *"skipped (autoreset budget spent"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 1 ]
}

@test "re-run failing rc 1 is terminal, not a loop restart" {
  python3 "$GATES" grant "$GSD_RUN_ID" --action wall-reset:04-foo --rollback "n/a"
  printf '3\n1\n' > "$WALL_RC_QUEUE"
  run -1 run_gate
  [[ "$output" == *"WALL-AUTO-CONTINUE exhausted (re-run rc=1)"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 2 ]
}

@test "missing GSD_RUN_ID fails closed" {
  printf '3\n' > "$WALL_RC_QUEUE"
  run -3 bash -c "unset GSD_RUN_ID; source '$GATE_SRC'; _gsd_run_wall_gate '$PHASE_DIR'"
  [[ "$output" == *"skipped (no GSD_RUN_ID)"* ]]
}

@test "resolved finding no longer blocks: adjudication clears the precondition" {
  out="$(python3 "$GATES" findings-queue add "f.py" "bad" --severity HIGH \
    --run-id "$GSD_RUN_ID" --source wall --plan ".planning/phases/04-foo/01-PLAN.md")"
  sig="$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  python3 "$GATES" findings-queue resolve "$sig" --disposition fix --reason "fixed in commit"
  python3 "$GATES" grant "$GSD_RUN_ID" --action wall-reset:04-foo --rollback "n/a"
  printf '3\n0\n' > "$WALL_RC_QUEUE"
  run -0 run_gate
  [[ "$output" == *"WALL-AUTO-CONTINUE phase=04-foo"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 2 ]
}

@test "unresolved CRITICAL blocks (severity CSV second element)" {
  python3 "$GATES" findings-queue add "f.py" "worse" --severity CRITICAL \
    --run-id "$GSD_RUN_ID" --source wall --plan ".planning/phases/04-foo/01-PLAN.md"
  python3 "$GATES" grant "$GSD_RUN_ID" --action wall-reset:04-foo --rollback "n/a"
  printf '3\n' > "$WALL_RC_QUEUE"
  run -3 run_gate
  [[ "$output" == *"unresolved HIGH/CRITICAL"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 1 ]
}

@test "PLAN.md fallback name is checked too" {
  rm "$PHASE_DIR/01-PLAN.md"
  printf '%s\n' plan > "$PHASE_DIR/PLAN.md"
  python3 "$GATES" findings-queue add "f.py" "bad" --severity HIGH \
    --run-id "$GSD_RUN_ID" --source wall --plan ".planning/phases/04-foo/PLAN.md"
  python3 "$GATES" grant "$GSD_RUN_ID" --action wall-reset:04-foo --rollback "n/a"
  printf '3\n' > "$WALL_RC_QUEUE"
  run -3 run_gate
  [[ "$output" == *"unresolved HIGH/CRITICAL"* ]]
}

@test "phase with no plan files fails closed even with a grant" {
  rm "$PHASE_DIR/01-PLAN.md"
  python3 "$GATES" grant "$GSD_RUN_ID" --action wall-reset:04-foo --rollback "n/a"
  printf '3\n' > "$WALL_RC_QUEUE"
  run -3 run_gate
  [[ "$output" == *"no enumerable plan files"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 1 ]
}

@test "garbage findings-queue output fails closed, not a crash" {
  mkdir -p "$WORK/packages/feature-fix-swarm/lib"
  cat > "$WORK/packages/feature-fix-swarm/lib/gates.py" <<'PYSTUB'
import sys
if "findings-queue" in sys.argv:
    print("TRACEBACK garbage not json")
    sys.exit(0)
sys.exit(0)
PYSTUB
  python3 "$GATES" grant "$GSD_RUN_ID" --action wall-reset:04-foo --rollback "n/a"
  printf '3\n' > "$WALL_RC_QUEUE"
  run -3 run_gate
  [[ "$output" == *"unresolved HIGH/CRITICAL"* ]]
  [ "$(grep -c called "$WALL_CALL_LOG")" -eq 1 ]
}
