#!/usr/bin/env bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/gsd/state-phase.sh"

@test "mid-phase status returns current phase minus one (frontmatter poisoned with 999s)" {
  FIXTURE="$BATS_TEST_TMPDIR/STATE.md"
  cat > "$FIXTURE" <<'EOF'
---
progress:
  completed_phases: 999
  percent: 999
---
## Current Position
Phase: 3 of 10
Status: In progress
EOF
  run bash "$SCRIPT" "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "2" ]
}

@test "phase-complete status returns current phase unchanged" {
  FIXTURE="$BATS_TEST_TMPDIR/STATE.md"
  cat > "$FIXTURE" <<'EOF'
---
progress:
  completed_phases: 999
  percent: 999
---
## Current Position
Phase: 3 of 10
Status: Phase complete — ready for verification
EOF
  run bash "$SCRIPT" "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "3" ]
}

@test "first-phase mid-phase status floors at zero" {
  FIXTURE="$BATS_TEST_TMPDIR/STATE.md"
  cat > "$FIXTURE" <<'EOF'
---
progress:
  completed_phases: 0
  percent: 0
---
## Current Position
Phase: 1 of 5
Status: In progress
EOF
  run bash "$SCRIPT" "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "0" ]
}

@test "frontmatter decoy Phase line is stripped, body value wins" {
  FIXTURE="$BATS_TEST_TMPDIR/STATE.md"
  cat > "$FIXTURE" <<'EOF'
---
progress:
  completed_phases: 999
  percent: 999
decoy_note: "Phase: 9 of 9"
---
## Current Position
Phase: 2 of 5
Status: In progress
EOF
  run bash "$SCRIPT" "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "1" ]
}

@test "missing state file exits 2 with not found message" {
  run bash "$SCRIPT" "$BATS_TEST_TMPDIR/nonexistent-STATE.md"
  [ "$status" -eq 2 ]
  [[ "$output" == *"not found"* ]]
}

@test "body without Phase line exits 1 with actionable message" {
  FIXTURE="$BATS_TEST_TMPDIR/STATE.md"
  cat > "$FIXTURE" <<'EOF'
---
progress:
  completed_phases: 0
  percent: 0
---
## Current Position
Status: In progress
EOF
  run bash "$SCRIPT" "$FIXTURE"
  [ "$status" -eq 1 ]
  [[ "$output" == *"no 'Phase: <number>'"* ]]
}

@test "zero args defaults to .planning/STATE.md" {
  mkdir -p "$BATS_TEST_TMPDIR/.planning"
  cat > "$BATS_TEST_TMPDIR/.planning/STATE.md" <<'EOF'
---
progress:
  completed_phases: 999
  percent: 999
---
## Current Position
Phase: 3 of 10
Status: In progress
EOF
  cd "$BATS_TEST_TMPDIR"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [ "$output" = "2" ]
}

@test "two or more args exits 2 with usage message" {
  run bash "$SCRIPT" a b
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: state-phase.sh"* ]]
}

@test "real-template mid-phase body (frontmatter poisoned with 999s)" {
  FIXTURE="$BATS_TEST_TMPDIR/STATE.md"
  cat > "$FIXTURE" <<'EOF'
---
progress:
  completed_phases: 999
  percent: 999
---
## Current Position

Phase: 01 (support-scripts) — EXECUTING
Plan: 1 of 2
Status: Executing Phase 01
Last activity: 2026-07-05 — Phase 01 execution started
EOF
  run bash "$SCRIPT" "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "0" ]
}

@test "real-template phase-complete body with zero-padded Phase number" {
  FIXTURE="$BATS_TEST_TMPDIR/STATE.md"
  cat > "$FIXTURE" <<'EOF'
---
progress:
  completed_phases: 999
  percent: 999
---
## Current Position

Phase: 02 (support-scripts) — EXECUTING
Plan: 2 of 2
Status: Phase complete — ready for verification
Last activity: 2026-07-05 — Phase 01 execution started
EOF
  run bash "$SCRIPT" "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "2" ]
}
