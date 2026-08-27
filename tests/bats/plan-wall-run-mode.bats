#!/usr/bin/env bats
# plan-wall.sh --run — run-level wall (D3, operator decision 2026-08-27):
# wall every planned phase dir under a root in ONE invocation, under ONE
# global `wall:run` round counter. Per-plan records stay keyed by each
# plan's real parent phase slug (gates-test-command.sh per-phase completion
# backstop must clear unchanged), residuals land per phase dir, plan-less
# subdirs are skipped, and a per-phase re-entry after a run-level pass takes
# the sha-unchanged zero-dispatch idempotence path.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LEVER="$ROOT/scripts/gsd/plan-wall.sh"
  BACKSTOP="$ROOT/scripts/gsd/gates-test-command.sh"
  REAL_GATES_PY="$ROOT/lib/gates.py"
  REAL_SCHEMA="$ROOT/schemas/review-finding.schema.json"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/packages/feature-fix-swarm/lib" "$REPO/schemas" "$REPO/bin"
  cp "$REAL_GATES_PY" "$REPO/packages/feature-fix-swarm/lib/gates.py"
  cp "$REAL_SCHEMA" "$REPO/schemas/review-finding.schema.json"
  cd "$REPO"
  git init -q -b main
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  mkdir -p .planning/phases/01-alpha .planning/phases/02-beta .planning/phases/03-empty
  cat > .planning/config.json <<'JSON'
{"model_overrides": {"gsd-planner": "fable"}, "dynamic_routing": {"escalate_on_failure": true}}
JSON
  echo "Phase alpha: widget with ALPHAMARK content" > .planning/phases/01-alpha/01-PLAN.md
  echo "Phase beta: plain widget, nothing sensitive" > .planning/phases/02-beta/PLAN.md
  export PATH="$REPO/bin:$PATH"
  export FFS_ADVERSARY_MODEL_PROBE=off
  export GATES_PY="$REPO/packages/feature-fix-swarm/lib/gates.py"
  export GATES_STORE="$REPO/.feature-fix-swarm/evidence.json"
  export GSD_RUN_ID="spec-run-test"
  export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz
  export ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz
}

# stub reviewer: HIGH finding only for the plan containing ALPHAMARK,
# clean pass for everything else — distinguishes per-phase residual routing
stub_alpha_high() {
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
if grep -q ALPHAMARK; then
  printf '{"findings":[{"severity":"HIGH","file":"a.py","claim":"missing null check"}]}\n'
else
  printf '{"findings":[]}\n'
fi
EOF
  chmod +x bin/stub-claude
}

stub_critical_everywhere() {
  cat > bin/stub-claude <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
printf '{"findings":[{"severity":"CRITICAL","file":"a.py","claim":"auth bypass"}]}\n'
EOF
  chmod +x bin/stub-claude
}

run_wall_run() {
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" --run
}

@test "--run walls every planned phase, keys records by real phase slug, skips plan-less dirs" {
  stub_alpha_high
  run_wall_run
  [ "$status" -eq 0 ]
  [[ "$output" == *"PLAN-WALL-PASS-RESIDUAL"* ]]
  # per-phase record keys — the gates-test-command glob shape
  ls .planning/run-state/plan-wall-01-alpha-*.json
  ls .planning/run-state/plan-wall-02-beta-*.json
  # plan-less dir produced no record
  ! ls .planning/run-state/plan-wall-03-empty-*.json 2>/dev/null
  # residuals grouped per parent phase dir
  [ -f .planning/phases/01-alpha/WALL-RESIDUALS.md ]
  grep -q "missing null check" .planning/phases/01-alpha/WALL-RESIDUALS.md
  [ ! -f .planning/phases/02-beta/WALL-RESIDUALS.md ]
  # verdicts: alpha rides residual, beta clean
  [ "$(jq -r '.verdict' .planning/run-state/plan-wall-01-alpha-*.json)" = "pass-residual" ]
  [ "$(jq -r '.verdict' .planning/run-state/plan-wall-02-beta-*.json)" = "reviewed-pass" ]
}

@test "real gates-test-command.sh clears each phase from run-mode records" {
  stub_alpha_high
  run_wall_run
  [ "$status" -eq 0 ]
  GSD_TEST_CMD=true run bash "$BACKSTOP" 01-alpha
  [ "$status" -eq 0 ]
  [[ "$output" != *"BACKSTOP"* ]]
  GSD_TEST_CMD=true run bash "$BACKSTOP" 02-beta
  [ "$status" -eq 0 ]
  [[ "$output" != *"BACKSTOP"* ]]
  # a phase the run never walled still fails closed
  GSD_TEST_CMD=true run bash "$BACKSTOP" 03-empty
  [ "$status" -ne 0 ]
  [[ "$output" == *"BACKSTOP"* ]]
}

@test "per-phase re-entry after a run pass is zero-dispatch idempotent" {
  stub_alpha_high
  run_wall_run
  [ "$status" -eq 0 ]
  MARKER="$BATS_TEST_TMPDIR/should-not-run"
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
touch "$MARKER"
cat >/dev/null
printf '{"findings":[]}\n'
EOF
  chmod +x bin/stub-claude
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/01-alpha
  [ "$status" -eq 0 ]
  [ ! -f "$MARKER" ]
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/02-beta
  [ "$status" -eq 0 ]
  [ ! -f "$MARKER" ]
}

@test "--run --await is refused (usage, exit 2) in both flag orders" {
  run bash "$LEVER" --run --await
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage"* ]]
  run bash "$LEVER" --await 30 --run
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage"* ]]
}

@test "zero plans under the root exits 2, not a silent pass" {
  rm .planning/phases/01-alpha/01-PLAN.md .planning/phases/02-beta/PLAN.md
  stub_alpha_high
  run_wall_run
  [ "$status" -eq 2 ]
  [[ "$output" == *"zero plan files"* ]]
}

@test "missing phases root exits 2" {
  run bash "$LEVER" --run .planning/nonexistent
  [ "$status" -eq 2 ]
}

@test "wall:run counter resets on pass — repeated passing runs never cap" {
  stub_alpha_high
  for _ in 1 2 3 4 5; do
    run_wall_run
    [ "$status" -eq 0 ]
    [[ "$output" != *"WALL-ROUND-CAP"* ]]
  done
}

@test "run-level CRITICAL blocks round 1, quarantines on the final round" {
  stub_critical_everywhere
  run_wall_run
  [ "$status" -eq 1 ]
  [[ "$output" == *"BLOCKED --run"* ]]
  [[ "$output" == *"one repair round remains"* ]]
  run_wall_run
  [ "$status" -eq 3 ]
  [[ "$output" == *"WALL-ROUND-CAP --run"* ]]
}

@test "--run does not consume per-phase round counters" {
  stub_critical_everywhere
  run_wall_run
  [ "$status" -eq 1 ]
  # per-phase counters untouched: a direct per-phase invocation still starts
  # at round 1 (blocked, NOT capped)
  stub_critical_everywhere
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude run bash "$LEVER" .planning/phases/02-beta
  [ "$status" -eq 1 ]
  [[ "$output" != *"WALL-ROUND-CAP"* ]]
}

@test "--run default root honors GSD_PROJECT planning isolation" {
  # consumer layout: .planning/<project>/phases/<phase>/
  mkdir -p ".planning/projA/phases/01-iso"
  echo "Phase iso: a plain widget under project isolation" > .planning/projA/phases/01-iso/PLAN.md
  stub_alpha_high
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude GSD_PROJECT=projA \
    run bash "$LEVER" --run
  [ "$status" -eq 0 ]
  # walled the isolated phase, NOT the top-level ones
  ls .planning/run-state/plan-wall-01-iso-*.json
  ! ls .planning/run-state/plan-wall-01-alpha-*.json 2>/dev/null
}

@test "--run with GSD_PROJECT set but no isolated dir falls back to the plain root" {
  stub_alpha_high
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude GSD_PROJECT=absent \
    run bash "$LEVER" --run
  [ "$status" -eq 0 ]
  ls .planning/run-state/plan-wall-01-alpha-*.json
}

@test "--run explicit root arg beats GSD_PROJECT" {
  mkdir -p ".planning/projA/phases/01-iso"
  echo "Phase iso: a plain widget" > .planning/projA/phases/01-iso/PLAN.md
  stub_alpha_high
  FFS_HOST=claude ADVERSARY_BIN_CLAUDE=stub-claude GSD_PROJECT=projA \
    run bash "$LEVER" --run .planning/phases
  [ "$status" -eq 0 ]
  ls .planning/run-state/plan-wall-01-alpha-*.json
  ! ls .planning/run-state/plan-wall-01-iso-*.json 2>/dev/null
}
