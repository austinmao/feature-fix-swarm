#!/usr/bin/env bats
# plan-wall.sh one-round pass criterion — wall policy (b), operator decision
# 2026-08-27 (supersedes 2026-08-08 diminishing-returns, which never
# converged in four independent live runs): round 1 is terminal. Zero
# unresolved CRITICAL -> PASS immediately; unresolved HIGHs ride into
# execution as pinned executor assumptions (left unresolved in
# findings-queue, surfaced via WALL-RESIDUALS.md, closed by the
# executed-diff review — policy (c)). An unresolved CRITICAL blocks and buys
# exactly ONE repair round; a CRITICAL surviving that round quarantines with
# WALL-ROUND-CAP (exit 3). Separate file from plan-wall.bats: these tests
# need a STABLE GSD_RUN_ID so the round counter accumulates across
# invocations.

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

@test "round-1 HIGH-only passes immediately as PASS-RESIDUAL — residuals ride unresolved" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"missing null check"},{"severity":"HIGH","file":"b.py","claim":"race on init"}]'
  run_wall
  [ "$status" -eq 0 ]
  [[ "$output" == *"PLAN-WALL-PASS-RESIDUAL"* ]]
  # residuals ride: nothing was auto-resolved
  [ "$(queue_unresolved_count)" = "2" ]
  # residual manifest exists and names the findings for the executor + diff review
  [ -f .planning/phases/1-foo/WALL-RESIDUALS.md ]
  grep -q "missing null check" .planning/phases/1-foo/WALL-RESIDUALS.md
  grep -q "race on init" .planning/phases/1-foo/WALL-RESIDUALS.md
  # per-plan record carries the residual verdict, not "blocked"
  [ "$(jq -r '.verdict' .planning/run-state/plan-wall-1-foo-plan.json)" = "pass-residual" ]
}

@test "round-1 CRITICAL blocks with one repair round remaining" {
  stub_claude_json '[{"severity":"CRITICAL","file":"d.py","claim":"auth bypass"}]'
  run_wall
  [ "$status" -eq 1 ]
  [[ "$output" == *"BLOCKED"* ]]
  [[ "$output" == *"one repair round remains"* ]]
  [[ "$output" != *"PLAN-WALL-PASS-RESIDUAL"* ]]
  [ ! -f .planning/phases/1-foo/WALL-RESIDUALS.md ]
}

@test "CRITICAL surviving the repair round quarantines — WALL-ROUND-CAP exit 3" {
  stub_claude_json '[{"severity":"CRITICAL","file":"d.py","claim":"auth bypass"}]'
  run_wall
  [ "$status" -eq 1 ]
  # round 2 (default max): CRITICAL still unresolved -> quarantine now,
  # NOT another retryable BLOCKED
  run_wall
  [ "$status" -eq 3 ]
  [[ "$output" == *"WALL-ROUND-CAP"* ]]
  [[ "$output" == *"findings-queue list --unresolved"* ]]
  [[ "$output" == *"--reset"* ]]
}

@test "repair round with CRITICAL resolved and HIGHs open passes as PASS-RESIDUAL" {
  stub_claude_json '[{"severity":"CRITICAL","file":"d.py","claim":"auth bypass"},{"severity":"HIGH","file":"a.py","claim":"missing null check"}]'
  run_wall
  [ "$status" -eq 1 ]
  # operator resolves the CRITICAL between rounds (the repair)
  sig="$(python3 "$GATES_PY" findings-queue list --unresolved --source wall \
    --severity CRITICAL --plan .planning/phases/1-foo/PLAN.md | jq -r '.[0].sig')"
  python3 "$GATES_PY" findings-queue resolve "$sig" --disposition fix --reason "fixed in plan"
  # round 2: reviewer re-reports the HIGH (dedup -> residual, not new)
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"missing null check"}]'
  run_wall
  [ "$status" -eq 0 ]
  [[ "$output" == *"PLAN-WALL-PASS-RESIDUAL"* ]]
  [ -f .planning/phases/1-foo/WALL-RESIDUALS.md ]
  grep -q "missing null check" .planning/phases/1-foo/WALL-RESIDUALS.md
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

@test "passing wall re-runs never exhaust the round cap — pass resets the counter" {
  stub_claude_json '[{"severity":"HIGH","file":"a.py","claim":"one"}]'
  run_wall
  [ "$status" -eq 0 ]
  # a runner/orchestrator may re-invoke the wall on every attempt; four
  # more idempotent passes must never trip WALL-ROUND-CAP (default max 2)
  for _ in 1 2 3 4; do
    run_wall
    [ "$status" -eq 0 ]
    [[ "$output" != *"WALL-ROUND-CAP"* ]]
  done
}

@test "reviewer prompt pins the CRITICAL severity discipline (sole blocking severity)" {
  # Under the one-round policy CRITICAL is the ONLY severity that blocks, so
  # a loose definition would re-inflate exactly the churn this policy cuts.
  # Ported from openclaw's independent plan-wall fork (#1784 cluster).
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
cat > "$BATS_TEST_TMPDIR/captured-prompt.txt"
printf '{"findings":[]}\n'
EOF
  chmod +x bin/stub-claude
  run_wall
  [ "$status" -eq 0 ]
  grep -q "CRITICAL is reserved for a defect" "$BATS_TEST_TMPDIR/captured-prompt.txt"
  grep -q "irreversible loss" "$BATS_TEST_TMPDIR/captured-prompt.txt"
  grep -q "cite the exact plan step" "$BATS_TEST_TMPDIR/captured-prompt.txt"
  grep -q "every other defect is at most HIGH" "$BATS_TEST_TMPDIR/captured-prompt.txt"
}
