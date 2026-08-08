#!/usr/bin/env bats
# plan-wall.sh diminishing-returns pass criterion — wall policy (b), operator
# decision 2026-08-08: the wall passes when a round reports ZERO unresolved
# CRITICAL and STRICTLY FEWER new HIGH/CRITICAL findings than the previous
# round for the same phase; residual HIGHs ride into execution as pinned
# executor assumptions (left unresolved in findings-queue, surfaced via
# WALL-RESIDUALS.md, closed by the executed-diff review — policy (c)).
# Separate file from plan-wall.bats: these tests need a STABLE GSD_RUN_ID so
# round/count history accumulates across invocations (plan-wall.bats runs
# derive an ephemeral run id per invocation, which keeps its old tests on
# first-round-strict behavior by construction).

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
  export GSD_RUN_ID="spec-dim-test"
  export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz
  export ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz
}

stub_claude_json() {
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' '{"findings": $1}'
EOF
  chmod +x bin/stub-claude
}

run_wall() {
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/1-foo
}

queue_unresolved_count() {
  python3 "$GATES_PY" findings-queue list --unresolved --source wall \
    --severity HIGH,CRITICAL --plan .planning/phases/1-foo/PLAN.md | jq 'length'
}

@test "first round with findings is BLOCKED — no previous round to compare (strict rule)" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"missing null check"}]'
  run_wall
  [ "$status" -eq 1 ]
  [[ "$output" == *"BLOCKED"* ]]
  [[ "$output" != *"PLAN-WALL-PASS-RESIDUAL"* ]]
  [ ! -f .planning/phases/1-foo/WALL-RESIDUALS.md ]
}

@test "strict decrease + zero CRITICAL passes with residuals riding unresolved" {
  # round 1: two new HIGH -> blocked (first-round strict)
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"missing null check"},{"severity":"HIGH","file":"b.py","claim":"race on init"}]'
  run_wall
  [ "$status" -eq 1 ]
  # round 2: ONE new HIGH (old ones re-reported -> deduped, not new) -> 1 < 2
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"missing null check"},{"severity":"HIGH","file":"c.py","claim":"unbounded retry"}]'
  run_wall
  [ "$status" -eq 0 ]
  [[ "$output" == *"PLAN-WALL-PASS-RESIDUAL"* ]]
  # residuals ride: nothing was auto-resolved
  [ "$(queue_unresolved_count)" = "3" ]
  # residual manifest exists and names the findings for the executor + diff review
  [ -f .planning/phases/1-foo/WALL-RESIDUALS.md ]
  grep -q "unbounded retry" .planning/phases/1-foo/WALL-RESIDUALS.md
  grep -q "missing null check" .planning/phases/1-foo/WALL-RESIDUALS.md
  # per-plan record carries the residual verdict, not "blocked"
  [ "$(jq -r '.verdict' .planning/run-state/plan-wall-1-foo-plan.json)" = "pass-residual" ]
}

@test "equal new-finding count is BLOCKED — not converging" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"missing null check"},{"severity":"HIGH","file":"b.py","claim":"race on init"}]'
  run_wall
  [ "$status" -eq 1 ]
  stub_claude_json '[{"severity":"HIGH","file":"c.py","claim":"unbounded retry"},{"severity":"HIGH","file":"d.py","claim":"toctou on lockfile"}]'
  run_wall
  [ "$status" -eq 1 ]
  [[ "$output" == *"BLOCKED"* ]]
  [[ "$output" != *"PLAN-WALL-PASS-RESIDUAL"* ]]
}

@test "unresolved CRITICAL always blocks — even on a strict decrease" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"one"},{"severity":"HIGH","file":"b.py","claim":"two"},{"severity":"HIGH","file":"c.py","claim":"three"}]'
  run_wall
  [ "$status" -eq 1 ]
  stub_claude_json '[{"severity":"CRITICAL","file":"d.py","claim":"auth bypass"}]'
  run_wall
  [ "$status" -eq 1 ]
  [[ "$output" != *"PLAN-WALL-PASS-RESIDUAL"* ]]
}

@test "zero findings still REVIEWED-PASS unchanged with the counter active" {
  stub_claude_json '[]'
  run_wall
  [ "$status" -eq 0 ]
  [[ "$output" == *"REVIEWED-PASS"* ]]
}

@test "re-run after pass-residual on unchanged plan is idempotent — zero dispatch" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"one"},{"severity":"HIGH","file":"b.py","claim":"two"}]'
  run_wall
  [ "$status" -eq 1 ]
  stub_claude_json '[{"severity":"HIGH","file":"c.py","claim":"three"}]'
  run_wall
  [ "$status" -eq 0 ]
  [[ "$output" == *"PLAN-WALL-PASS-RESIDUAL"* ]]
  MARKER="$BATS_TEST_TMPDIR/should-not-run"
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
touch "$MARKER"
cat >/dev/null
printf '{"findings":[]}\n'
EOF
  chmod +x bin/stub-claude
  run_wall
  [ "$status" -eq 0 ]
  [[ "$output" == *"PASS-RESIDUAL"* ]]
  [ ! -f "$MARKER" ]
}

@test "loop-round --reset clears count history — post-reset round is first-round strict" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"one"},{"severity":"HIGH","file":"b.py","claim":"two"}]'
  run_wall
  [ "$status" -eq 1 ]
  python3 "$GATES_PY" loop-round "$GSD_RUN_ID" "wall:1-foo" --reset
  # a decrease vs the PRE-reset round must NOT pass: history is gone
  stub_claude_json '[{"severity":"HIGH","file":"c.py","claim":"three"}]'
  run_wall
  [ "$status" -eq 1 ]
  [[ "$output" != *"PLAN-WALL-PASS-RESIDUAL"* ]]
}

@test "passing wall re-runs never exhaust the round cap — pass resets the counter" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"one"},{"severity":"HIGH","file":"b.py","claim":"two"}]'
  run_wall
  [ "$status" -eq 1 ]
  stub_claude_json '[{"severity":"HIGH","file":"c.py","claim":"three"}]'
  run_wall
  [ "$status" -eq 0 ]
  # a runner/orchestrator may re-invoke the wall on every attempt; three
  # more idempotent passes must never trip WALL-ROUND-CAP (default max 3)
  for _ in 1 2 3 4; do
    run_wall
    [ "$status" -eq 0 ]
    [[ "$output" != *"WALL-ROUND-CAP"* ]]
  done
}
