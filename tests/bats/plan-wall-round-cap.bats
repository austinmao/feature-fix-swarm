#!/usr/bin/env bats
# plan-wall.sh round cap (2026-08 red-team G3): a durable per-phase counter
# bounds wall invocations per run. Deliberately a SEPARATE file from
# plan-wall.bats (PR #88 rewrites that file's stubs; these tests are
# shape-agnostic and must not conflict). Reviewer CLIs resolve nowhere, so
# every dispatch fails fast (WALL-UNREVIEWED, exit 1) — the assertions here
# only distinguish exit 3 (WALL-ROUND-CAP) from everything else, so they
# survive any findings-shape change.

bats_require_minimum_version 1.5.0

setup() {
  LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/plan-wall.sh"
  REAL_GATES_PY="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/lib/gates.py"
  REAL_SCHEMA="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/schemas/review-finding.schema.json"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/packages/feature-fix-swarm/lib" "$REPO/schemas" "$REPO/bin"
  cp "$REAL_GATES_PY" "$REPO/packages/feature-fix-swarm/lib/gates.py"
  cp "$REAL_SCHEMA" "$REPO/schemas/review-finding.schema.json"
  cd "$REPO"
  git init -q -b main
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  mkdir -p .planning/phases/1-foo
  cat > .planning/config.json <<'JSON'
{"model_overrides": {"gsd-planner": "fable"}, "dynamic_routing": {"escalate_on_failure": true}}
JSON
  echo "Phase 1: build a plain widget, nothing sensitive here" > .planning/phases/1-foo/PLAN.md
  export PATH="$REPO/bin:$PATH"
  export FFS_ADVERSARY_MODEL_PROBE=off
  export GATES_PY="$REPO/packages/feature-fix-swarm/lib/gates.py"
  export GATES_STORE="$REPO/.feature-fix-swarm/evidence.json"
  export GSD_RUN_ID="spec-cap-test"
  # both vendors resolve nowhere -> every rung fails fast, no real CLI calls
  export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz
  export ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz
}

run_wall() {
  run bash "$LEVER" .planning/phases/1-foo
}

@test "round cap: default 2 — hard block on the second invocation exits 3 with WALL-ROUND-CAP" {
  run_wall; [ "$status" -ne 3 ]
  run_wall
  [ "$status" -eq 3 ]
  [[ "$output" == *"WALL-ROUND-CAP"* ]]
  # the unblock is printed, naming both levers
  [[ "$output" == *"findings-queue list --unresolved"* ]]
  [[ "$output" == *"--reset"* ]]
}

@test "round cap: PLAN_WALL_MAX_ROUNDS=1 — hard block on the FIRST invocation quarantines (no repair round)" {
  PLAN_WALL_MAX_ROUNDS=1 run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 3 ]
  [[ "$output" == *"WALL-ROUND-CAP"* ]]
}

@test "round cap: counter is durable across invocations but reset-all clears it" {
  run_wall
  run_wall
  [ "$status" -eq 3 ]
  python3 "$GATES_PY" loop-round "$GSD_RUN_ID" --reset-all
  # post-reset the phase is back on round 1 of 2 — blocked, not capped
  run_wall
  [ "$status" -ne 3 ]
}

@test "round cap: PLAN_WALL=off waiver never consumes a round" {
  for _ in 1 2 3 4 5; do
    PLAN_WALL=off run bash "$LEVER" .planning/phases/1-foo
    [ "$status" -ne 3 ]
  done
  # first real invocation is round 1, not capped
  run_wall
  [ "$status" -ne 3 ]
}

@test "round cap: garbage PLAN_WALL_MAX_ROUNDS falls back to default 2" {
  PLAN_WALL_MAX_ROUNDS=banana run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -ne 3 ]
  PLAN_WALL_MAX_ROUNDS=banana run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 3 ]
}

@test "round cap: counter infrastructure failure fails OPEN, never impersonates the cap" {
  # An empty read-only store makes loop-round's _load_store raise
  # (JSONDecodeError) — rc=3 LOOP-ROUND-ERROR. The wall must WARN and
  # proceed unbounded (the queue_error path downstream owns broken-store
  # reporting), never exit 3. Regression for PR #91 round 1: any nonzero
  # from loop-round was read as cap-hit, breaking plan-wall.bats' own
  # queue-I/O fault-injection case.
  mkdir -p "$(dirname "$GATES_STORE")"
  : > "$GATES_STORE"
  chmod 444 "$GATES_STORE"
  run bash "$LEVER" .planning/phases/1-foo
  chmod 644 "$GATES_STORE"
  [ "$status" -ne 3 ]
  [[ "$output" == *"round counter unavailable"* ]]
  [[ "$output" != *"WALL-ROUND-CAP"* ]]
}

@test "round cap: distinct phases count independently" {
  mkdir -p .planning/phases/2-bar
  echo "Phase 2: another plain widget" > .planning/phases/2-bar/PLAN.md
  run_wall
  run_wall
  [ "$status" -eq 3 ]
  # phase 2 is untouched by phase 1's exhausted counter
  run bash "$LEVER" .planning/phases/2-bar
  [ "$status" -ne 3 ]
}
