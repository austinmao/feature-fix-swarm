#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  GATE="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/plan-length-gate.sh"
  PHASE="$BATS_TEST_TMPDIR/phase"
  mkdir -p "$PHASE"
}

write_lines() { yes x | head -n "$2" > "$PHASE/$1"; }

@test "reports a 301-line plan with exact colon-delimited shape" {
  write_lines 01-01-PLAN.md 301
  run bash "$GATE" "$PHASE"
  [ "$status" -eq 1 ]; [[ "$output" =~ PLAN-LENGTH:.*01-01-PLAN.md:301:300 ]]
}

@test "accepts inclusive 300 limit and a single plan file" {
  write_lines PLAN.md 300
  run bash "$GATE" "$PHASE/PLAN.md"
  [ "$status" -eq 0 ]; [ -z "$output" ]
}

@test "reports every violation, handles empty input, and honors the override" {
  write_lines 01-01-PLAN.md 301; write_lines 01-02-PLAN.md 302
  run bash "$GATE" "$PHASE"
  [ "$status" -eq 1 ]; [[ "$output" == *01-01-PLAN.md* ]]; [[ "$output" == *01-02-PLAN.md* ]]
  empty="$BATS_TEST_TMPDIR/empty"; mkdir "$empty"
  run bash "$GATE" "$empty"
  [ "$status" -eq 1 ]; [[ "$output" == PLAN-LENGTH:no-plans* ]]
  write_lines PLAN.md 11
  run env FFS_PLAN_MAX_LINES=10 bash "$GATE" "$PHASE/PLAN.md"
  [ "$status" -eq 1 ]; [[ "$output" == *:11:10 ]]
}
