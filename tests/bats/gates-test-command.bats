#!/usr/bin/env bats
# gates-test-command.sh — spec-004 AC-005 completion backstop (INT-007).
# Asserts a plan-wall record exists for the executing phase with an accepted
# verdict (reviewed-pass|adjudicated-pass|WAIVED) before letting the
# gsd-core workflow.test_command gate run at all. Silent no-op when
# .planning/run-state/ is entirely absent (byte-identical prior behaviour —
# covers non-gsd callers and repos that never ran plan-wall.sh).

bats_require_minimum_version 1.5.0

setup() {
  LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/gates-test-command.sh"
  REAL_GATES_PY="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/lib/gates.py"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/lib"
  cp "$REAL_GATES_PY" "$REPO/lib/gates.py"
  cd "$REPO"
  git init -q -b main
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  export GATES_STORE="$BATS_TEST_TMPDIR/evidence.json"
  export GSD_TEST_CMD="true"
}

write_record() {
  # write_record <phase> <plan-slug> <verdict>
  mkdir -p .planning/run-state
  printf '{"verdict": "%s", "plan_sha256": "abc"}' "$3" \
    > ".planning/run-state/plan-wall-$1-$2.json"
}

@test "no .planning/run-state/ at all -> silent no-op, normal pass-through" {
  run bash "$LEVER" phase-1
  [ "$status" -eq 0 ]
  [[ "$output" != *"BACKSTOP"* ]]
}

@test "run-state dir exists but no record for this phase -> non-zero, named" {
  mkdir -p .planning/run-state
  run bash "$LEVER" phase-1
  [ "$status" -ne 0 ]
  [[ "$output" == *"BACKSTOP"* ]]
  [[ "$output" == *"phase-1"* ]]
}

@test "record exists for a DIFFERENT phase -> still absent for this phase, non-zero" {
  write_record "phase-2" "PLAN" "reviewed-pass"
  run bash "$LEVER" phase-1
  [ "$status" -ne 0 ]
  [[ "$output" == *"BACKSTOP"* ]]
}

@test "test command never runs when the backstop blocks (fail BEFORE test execution)" {
  SENTINEL="$BATS_TEST_TMPDIR/ran.sentinel"
  export GSD_TEST_CMD="touch '$SENTINEL'"
  mkdir -p .planning/run-state
  run bash "$LEVER" phase-1
  [ "$status" -ne 0 ]
  [ ! -f "$SENTINEL" ]
}

@test "verdict reviewed-pass -> backstop clears, normal gate runs and passes" {
  write_record "phase-1" "PLAN" "reviewed-pass"
  run bash "$LEVER" phase-1
  [ "$status" -eq 0 ]
  [[ "$output" != *"BACKSTOP"* ]]
}

@test "verdict adjudicated-pass -> backstop clears" {
  write_record "phase-1" "PLAN" "adjudicated-pass"
  run bash "$LEVER" phase-1
  [ "$status" -eq 0 ]
}

@test "verdict WAIVED -> backstop clears (a recorded waiver still completes the phase)" {
  write_record "phase-1" "PLAN" "WAIVED"
  run bash "$LEVER" phase-1
  [ "$status" -eq 0 ]
}

@test "verdict blocked -> backstop blocks with named record path" {
  write_record "phase-1" "PLAN" "blocked"
  run bash "$LEVER" phase-1
  [ "$status" -ne 0 ]
  [[ "$output" == *"BACKSTOP"* ]]
  [[ "$output" == *"plan-wall-phase-1-PLAN.json"* ]]
}

@test "verdict WALL-UNREVIEWED -> backstop blocks" {
  write_record "phase-1" "PLAN" "WALL-UNREVIEWED"
  run bash "$LEVER" phase-1
  [ "$status" -ne 0 ]
}

@test "multi-plan phase: one accepted + one blocked record -> aggregate blocks" {
  write_record "phase-1" "01-PLAN" "reviewed-pass"
  write_record "phase-1" "02-PLAN" "blocked"
  run bash "$LEVER" phase-1
  [ "$status" -ne 0 ]
  [[ "$output" == *"02-PLAN"* ]]
}

@test "multi-plan phase: all accepted (mixed verdicts) -> passes" {
  write_record "phase-1" "01-PLAN" "reviewed-pass"
  write_record "phase-1" "02-PLAN" "WAIVED"
  write_record "phase-1" "03-PLAN" "adjudicated-pass"
  run bash "$LEVER" phase-1
  [ "$status" -eq 0 ]
}

@test "a corrupt/unreadable record is treated as a bad verdict, not a crash" {
  mkdir -p .planning/run-state
  printf 'not json' > .planning/run-state/plan-wall-phase-1-PLAN.json
  run bash "$LEVER" phase-1
  [ "$status" -ne 0 ]
  [[ "$output" == *"BACKSTOP"* ]]
}

@test "phase id resolution: GSD_PHASE_ID env used when no positional arg given" {
  write_record "phase-env" "PLAN" "reviewed-pass"
  GSD_PHASE_ID=phase-env run bash "$LEVER"
  [ "$status" -eq 0 ]
}

@test "no arg, no GSD_PHASE_ID, run-state empty -> unscoped BACKSTOP blocks (not literal 'gsd-phase' key)" {
  mkdir -p .planning/run-state
  unset GSD_PHASE_ID
  run bash "$LEVER"
  [ "$status" -ne 0 ]
  [[ "$output" == *"BACKSTOP"* ]]
  [[ "$output" == *"no phase id"* ]]
}

@test "no arg, no GSD_PHASE_ID, every record under run-state accepted -> unscoped backstop clears" {
  write_record "phase-1" "01-PLAN" "reviewed-pass"
  write_record "phase-2" "01-PLAN" "WAIVED"
  unset GSD_PHASE_ID
  run bash "$LEVER"
  [ "$status" -eq 0 ]
}

@test "no arg, no GSD_PHASE_ID, one unaccepted record anywhere under run-state -> unscoped backstop blocks" {
  write_record "phase-1" "01-PLAN" "reviewed-pass"
  write_record "phase-2" "01-PLAN" "blocked"
  unset GSD_PHASE_ID
  run bash "$LEVER"
  [ "$status" -ne 0 ]
  [[ "$output" == *"BACKSTOP"* ]]
}
