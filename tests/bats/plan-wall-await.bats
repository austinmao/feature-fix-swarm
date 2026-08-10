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
    run PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
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
  run PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]; [[ "$output" == *'WALL-AWAIT:pending'* ]]
  write_record reviewed-pass "$(shasum -a 256 .planning/phases/1-foo/PLAN.md | awk '{print $1}')" other-run
  run PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
  printf '{bad' > .planning/run-state/plan-wall-1-foo-plan.json
  run PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]; [[ "$output" == *'WALL-AWAIT:unreadable-record'* ]]
  jq -n --arg sha "$(shasum -a 256 .planning/phases/1-foo/PLAN.md | awk '{print $1}')" '{run_id:"spec-await",plan_sha256:$sha,verdict:"reviewed-pass"}' > .planning/run-state/plan-wall-1-foo-plan.json
  run PLAN_WALL_AWAIT_POLL=1 bash "$WALL" --await 1 .planning/phases/1-foo
  [ "$status" -eq 75 ]
}

@test "await rejects an empty phase immediately and resolves relative to repository root" {
  mkdir .planning/phases/empty
  run bash "$WALL" --await 5 .planning/phases/empty
  [ "$status" -eq 1 ]; [[ "$output" == *'WALL-AWAIT:no-plans'* ]]
  write_record reviewed-pass
  cd /; run bash "$WALL" --await 1 "$REPO/.planning/phases/1-foo"
  [ "$status" -eq 0 ]
}
