#!/usr/bin/env bats
# scope-drift-gate.sh — phase-boundary spec/plan drift check (advisory).
# Deterministic: diff-vs-declared-surface classification + goal re-anchor.
# Judge: one bounded LLM verdict via GSD_DRIFT_JUDGE_CMD test seam.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LEVER="$REPO_ROOT/scripts/gsd/scope-drift-gate.sh"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
         GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  unset GSD_DRIFT_GATE GSD_DRIFT_JUDGE_CMD || true

  WORK="$BATS_TEST_TMPDIR/work"
  git init -q -b main "$WORK"
  cd "$WORK"
  echo base > base.txt; git add base.txt; git commit -qm "base"
  git checkout -qb feat
  mkdir -p src docs .planning/run-state spike-results
  echo s > src/app.sh
  echo d > docs/notes.md
  echo p > .planning/state.md
  echo e > spike-results/manifest.json
  git add -A; git commit -qm "work"

  PLAN="$BATS_TEST_TMPDIR/PLAN.md"
  cat > "$PLAN" <<'EOF'
---
phase: 01-demo
plan: 01
files_modified:
  - src/app.sh
goal: keep the demo lever green
---
body
EOF
}

@test "kill-switch GSD_DRIFT_GATE=off -> no-op, exit 0" {
  GSD_DRIFT_GATE=off run bash "$LEVER" --base main --plan "$PLAN"
  [ "$status" -eq 0 ]
  [[ "$output" == *"disabled"* ]]
}

@test "all changed files declared -> on scope, re-anchor printed, exit 0" {
  git rm -q docs/notes.md; git commit -qm "narrow to declared"
  run bash "$LEVER" --base main --plan "$PLAN"
  [ "$status" -eq 0 ]
  [[ "$output" == *"PHASE GOAL: keep the demo lever green"* ]]
  [[ "$output" != *"DRIFT WARNING"* ]]
}

@test "undeclared files above threshold -> DRIFT WARNING listing them, still exit 0" {
  run bash "$LEVER" --base main --plan "$PLAN"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRIFT WARNING"* ]]
  [[ "$output" == *"docs/notes.md"* ]]
}

@test ".planning and spike-results excluded from classification" {
  run bash "$LEVER" --base main --plan "$PLAN"
  [ "$status" -eq 0 ]
  [[ "$output" != *".planning/state.md"* ]]
  [[ "$output" != *"spike-results/manifest.json"* ]]
}

@test "glob pattern in files_modified matches nested paths" {
  cat > "$PLAN" <<'EOF'
---
files_modified:
  - src/*
  - docs/*
goal: globs work
---
EOF
  run bash "$LEVER" --base main --plan "$PLAN"
  [ "$status" -eq 0 ]
  [[ "$output" != *"DRIFT WARNING"* ]]
}

@test "no plan file -> fail-open WARN, exit 0" {
  run bash "$LEVER" --base main --plan "$BATS_TEST_TMPDIR/missing-PLAN.md"
  [ "$status" -eq 0 ]
  [[ "$output" == *"no declared surface"* ]]
}

@test "judge ON-TRACK verdict surfaces, exit 0" {
  cat > "$BATS_TEST_TMPDIR/judge" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
echo "DRIFT-VERDICT: ON-TRACK"
EOF
  chmod +x "$BATS_TEST_TMPDIR/judge"
  GSD_DRIFT_JUDGE_CMD="$BATS_TEST_TMPDIR/judge" \
    run bash "$LEVER" --base main --plan "$PLAN" --judge
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRIFT-VERDICT: ON-TRACK"* ]]
}

@test "judge DRIFT verdict -> loud advisory warning, still exit 0" {
  cat > "$BATS_TEST_TMPDIR/judge" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
echo "DRIFT-VERDICT: DRIFT — diff rewrites auth, phase goal is docs"
EOF
  chmod +x "$BATS_TEST_TMPDIR/judge"
  GSD_DRIFT_JUDGE_CMD="$BATS_TEST_TMPDIR/judge" \
    run bash "$LEVER" --base main --plan "$PLAN" --judge
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRIFT-VERDICT: DRIFT"* ]]
  [[ "$output" == *"advisory"* ]]
}

@test "judge command failure -> fail-soft WARN, exit 0" {
  printf '#!/usr/bin/env bash\nexit 1\n' > "$BATS_TEST_TMPDIR/judge"
  chmod +x "$BATS_TEST_TMPDIR/judge"
  GSD_DRIFT_JUDGE_CMD="$BATS_TEST_TMPDIR/judge" \
    run bash "$LEVER" --base main --plan "$PLAN" --judge
  [ "$status" -eq 0 ]
  [[ "$output" == *"judge unavailable"* ]]
}

@test "multiple --plan files union their declared surfaces" {
  PLAN2="$BATS_TEST_TMPDIR/PLAN2.md"
  cat > "$PLAN2" <<'EOF'
---
files_modified:
  - docs/notes.md
goal: second plan
---
EOF
  run bash "$LEVER" --base main --plan "$PLAN" --plan "$PLAN2"
  [ "$status" -eq 0 ]
  [[ "$output" != *"DRIFT WARNING"* ]]
}
