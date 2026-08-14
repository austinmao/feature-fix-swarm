#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  WALL="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/plan-wall.sh"
  REAL_GATES_PY="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/lib/gates.py"
  REAL_SCHEMA="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/schemas/review-finding.schema.json"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/packages/feature-fix-swarm/lib" "$REPO/schemas" "$REPO/bin" "$REPO/.planning/phases/1-foo" "$REPO/.planning/run-state"
  cp "$REAL_GATES_PY" "$REPO/packages/feature-fix-swarm/lib/gates.py"
  cp "$REAL_SCHEMA" "$REPO/schemas/review-finding.schema.json"
  cd "$REPO"; git init -q -b main; git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  printf 'plan body\n' > .planning/phases/1-foo/PLAN.md
  printf '{}' > .feature-fix-swarm-evidence.json
  export PATH="$REPO/bin:$PATH" GATES_PY="$REPO/packages/feature-fix-swarm/lib/gates.py" GATES_STORE="$REPO/.feature-fix-swarm-evidence.json" GSD_RUN_ID="spec-await"
  export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz
}

write_record() {
  local verdict="$1" sha="${2:-$(shasum -a 256 .planning/phases/1-foo/PLAN.md | awk '{print $1}')}" run_id="${3:-spec-await}"
  jq -n --arg verdict "$verdict" --arg sha "$sha" --arg run "$run_id" '{planner_model:"x",reviewer_model:"y",relation:"opposite",source_plan:".planning/phases/1-foo/PLAN.md",verdict:$verdict,plan_sha256:$sha,run_id:$run}' > .planning/run-state/plan-wall-1-foo-plan.json
}

@test "await returns done for each PASS-class record and is read-only" {
  for verdict in reviewed-pass adjudicated-pass pass-residual WAIVED; do
    write_record "$verdict"; cp "$GATES_STORE" before
    run env PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
    [ "$status" -eq 0 ]; [[ "$output" == *'WALL-AWAIT:done'* ]]; cmp before "$GATES_STORE"
  done
}

@test "await returns decided-blocked only for a trusted terminal record" {
  write_record blocked
  run bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 20 ]; [[ "$output" == *'WALL-AWAIT:decided-blocked'* ]]
}

@test "stale, foreign, malformed and forged records remain pending" {
  write_record reviewed-pass deadbeef
  run env PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]; [[ "$output" == *'WALL-AWAIT:pending'* ]]
  write_record reviewed-pass "$(shasum -a 256 .planning/phases/1-foo/PLAN.md | awk '{print $1}')" other-run
  run env PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
  printf '{bad' > .planning/run-state/plan-wall-1-foo-plan.json
  run env PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]; [[ "$output" == *'WALL-AWAIT:unreadable-record'* ]]
  jq -n --arg sha "$(shasum -a 256 .planning/phases/1-foo/PLAN.md | awk '{print $1}')" '{run_id:"spec-await",plan_sha256:$sha,verdict:"reviewed-pass"}' > .planning/run-state/plan-wall-1-foo-plan.json
  run env PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
}

@test "await rejects an empty phase immediately and resolves relative to repository root" {
  mkdir .planning/phases/empty
  run bash "$WALL" --await 5 .planning/phases/empty
  [ "$status" -eq 1 ]; [[ "$output" == *'WALL-AWAIT:no-plans'* ]]
  write_record reviewed-pass
  mkdir elsewhere; cd elsewhere; run bash "$WALL" --await 1 "$REPO/.planning/phases/1-foo"
  [ "$status" -eq 0 ]
}

@test "await classifies a WALL-ROUND-CAP verdict as decided-blocked (AC-001)" {
  write_record WALL-ROUND-CAP
  run bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 20 ]; [[ "$output" == *'WALL-AWAIT:decided-blocked'* ]]
}

@test "await accepts a socratic-folded plan_sha256 record (sha:socratic_sha)" {
  sha="$(shasum -a 256 .planning/phases/1-foo/PLAN.md | awk '{print $1}')"
  soc="$(printf 'socratic body' | shasum -a 256 | awk '{print $1}')"
  write_record reviewed-pass "${sha}:${soc}"
  run env PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 0 ]; [[ "$output" == *'WALL-AWAIT:done'* ]]
  write_record blocked "${sha}:${soc}"
  run bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 20 ]
  # a fold with the WRONG plan sha stays pending — forgery net intact
  write_record reviewed-pass "deadbeef:${soc}"
  run env PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
  # a fold whose suffix is not a 64-hex digest stays pending
  write_record reviewed-pass "${sha}:notasha"
  run env PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
}

@test "await enforces PLAN_WALL_AWAIT_MAX on pending returns, decided always wins" {
  rm -f .planning/run-state/plan-wall-1-foo-plan.json
  run env PLAN_WALL_AWAIT_MAX=2 PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
  run env PLAN_WALL_AWAIT_MAX=2 PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
  run env PLAN_WALL_AWAIT_MAX=2 PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 76 ]; [[ "$output" == *'WALL-AWAIT:attempts-exhausted'* ]]
  # a decided wall still reports through an exhausted counter and resets it
  write_record reviewed-pass
  run env PLAN_WALL_AWAIT_MAX=2 PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 0 ]; [[ "$output" == *'WALL-AWAIT:done'* ]]
  rm -f .planning/run-state/plan-wall-1-foo-plan.json
  run env PLAN_WALL_AWAIT_MAX=2 PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
}

@test "evaluator probe (PLAN_WALL_AWAIT_COUNT=off) is budget-neutral; counter is run-scoped" {
  rm -f .planning/run-state/plan-wall-1-foo-plan.json
  for i in 1 2 3 4; do
    run env PLAN_WALL_AWAIT_COUNT=off PLAN_WALL_AWAIT_MAX=2 PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
    [ "$status" -eq 75 ]
  done
  [ ! -f .planning/run-state/plan-wall-await-attempts-spec-await-1-foo ]
  # a poisoned counter from THIS run does not tax a different run id
  printf '99\n' > .planning/run-state/plan-wall-await-attempts-spec-await-1-foo
  run env GSD_RUN_ID=other-run PLAN_WALL_AWAIT_MAX=2 PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
  # while the current run is exhausted
  run env PLAN_WALL_AWAIT_MAX=2 PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 76 ]
}

@test "decided-blocked resets an exhausted attempts counter" {
  printf '99\n' > .planning/run-state/plan-wall-await-attempts-spec-await-1-foo
  write_record blocked
  run bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 20 ]
  [ ! -f .planning/run-state/plan-wall-await-attempts-spec-await-1-foo ]
}
