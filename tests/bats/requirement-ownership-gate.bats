#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/requirement-ownership-gate.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  PHASE_DIR="$REPO/.planning/phases/02-example"
  mkdir -p "$PHASE_DIR"
}

write_roadmap() {
  cat > "$REPO/.planning/ROADMAP.md" <<'EOF'
# Roadmap

## Phase Details

### Phase 1: Earlier
**Requirements**: REQ-OLD

### Phase 2: Example
**Goal**: Exercise exact ownership.
**Requirements**: FR-001, FR-002, FR-003

### Phase 3: Later
**Requirements**: REQ-LATER
EOF
}

write_plan() {
  local plan="$1" requirements="$2"
  cat > "$PHASE_DIR/02-${plan}-PLAN.md" <<EOF
---
phase: 02-example
plan: "${plan}"
type: execute
requirements: ${requirements}
must_haves:
  truths: []
---

# Plan ${plan}
EOF
}

@test "explicit empty preparatory plans and unique exact final ownership pass" {
  write_roadmap
  write_plan 01 '[] # preparatory plan'
  cat > "$PHASE_DIR/02-02-PLAN.md" <<'EOF'
---
phase: 02-example
plan: "02"
type: execute
requirements:
  - FR-001
  - "FR-002"
must_haves:
  truths: []
---
EOF
  write_plan 03 '[FR-003]'

  run bash -c "cd $(printf '%q' "$REPO") && bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 0 ]
  [[ "$output" == *"PASS phase=02 roadmap=3 plans=3"* ]]
}

@test "a phase requirement owned by multiple plans fails with both owners" {
  write_roadmap
  write_plan 01 '[FR-001, FR-002]'
  write_plan 02 '[FR-001, FR-003]'

  run bash -c "cd $(printf '%q' "$REPO") && bash $(printf '%q' "$SCRIPT") 02"

  [ "$status" -eq 2 ]
  [[ "$output" == *"FR-001 is owned by multiple plans"* ]]
  [[ "$output" == *"02-01-PLAN.md"* ]]
  [[ "$output" == *"02-02-PLAN.md"* ]]
}

@test "missing and non-roadmap ownership fail exact phase coverage" {
  write_roadmap
  write_plan 01 '[]'
  write_plan 02 '[FR-001, FR-999]'
  write_plan 03 '[FR-003]'

  run bash -c "cd $(printf '%q' "$REPO") && bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 2 ]
  [[ "$output" == *"missing from plans: FR-002"* ]]
  [[ "$output" == *"not owned by ROADMAP phase: FR-999"* ]]
}

@test "missing requirements field fails with explicit empty-list repair" {
  write_roadmap
  cat > "$PHASE_DIR/02-01-PLAN.md" <<'EOF'
---
phase: 02-example
plan: "01"
type: execute
must_haves:
  truths: []
---
EOF
  write_plan 02 '[FR-001, FR-002, FR-003]'

  run bash -c "cd $(printf '%q' "$REPO") && bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 2 ]
  [[ "$output" == *"02-01-PLAN.md has no explicit requirements field"* ]]
  [[ "$output" == *"use requirements: [] for preparatory plans"* ]]
}

@test "alternate ROADMAP bold-colon shape is accepted" {
  cat > "$REPO/.planning/ROADMAP.md" <<'EOF'
# Roadmap

## Phase 2: Example

**Requirements:** FR-001, FR-002
EOF
  write_plan 01 '[FR-001, FR-002]'

  run bash -c "cd $(printf '%q' "$REPO") && bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 0 ]
}

@test "missing phase requirements fail closed without waiting" {
  cat > "$REPO/.planning/ROADMAP.md" <<'EOF'
# Roadmap

## Phase 2: Example
**Goal**: No ownership declaration.
EOF
  write_plan 01 '[]'

  run bash -c "cd $(printf '%q' "$REPO") && bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 2 ]
  [[ "$output" == *"ROADMAP Phase 2 has no Requirements declaration"* ]]
}

@test "execution and planning skills pin last-completing-plan ownership" {
  grep -F 'requirement-ownership-gate.sh' "$ROOT/skills/feature-implement/SKILL.md"
  grep -F 'last plan that genuinely completes it' "$ROOT/skills/spec-decompose/SKILL.md"
  grep -F 'requirements: []' "$ROOT/skills/spec-decompose/SKILL.md"
}

# GSD_PROJECT planning isolation: a repo can hold several gsd projects
# (.planning/<project>/) next to a root one. The gate must check the project
# gsd-core will execute, never silently fall back to the root tree.
write_project() { # $1 = project id; phase 2 owns FR-101 in exactly one plan
  local dir="$REPO/.planning/$1/phases/02-scoped"
  mkdir -p "$dir"
  cat > "$REPO/.planning/$1/ROADMAP.md" <<'ROADMAP'
# Roadmap

### Phase 2: Scoped
**Requirements**: FR-101
ROADMAP
  cat > "$dir/02-01-PLAN.md" <<'PLAN'
---
phase: 02-scoped
plan: "01"
requirements: [FR-101]
---
PLAN
}

@test "GSD_PROJECT checks .planning/<project>/ and ignores the root project" {
  # The root project's phase 2 is broken (double ownership); the scoped one is clean.
  write_roadmap
  write_plan 01 '[FR-001, FR-002]'
  write_plan 02 '[FR-001, FR-003]'
  write_project 431

  run bash -c "cd $(printf '%q' "$REPO") && GSD_PROJECT=431 bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 0 ]
  [[ "$output" == *"PASS phase=02 roadmap=1 plans=1"* ]]
}

@test "unset GSD_PROJECT still checks the root project" {
  write_roadmap
  write_plan 01 '[FR-001, FR-002]'
  write_plan 02 '[FR-001, FR-003]'
  write_project 431

  run bash -c "cd $(printf '%q' "$REPO") && unset GSD_PROJECT && bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 2 ]
  [[ "$output" == *"FR-001 is owned by multiple plans"* ]]
}

@test "GSD_PROJECT without .planning/<project>/ROADMAP.md fails closed" {
  write_roadmap
  write_plan 01 '[FR-001, FR-002, FR-003]'

  run bash -c "cd $(printf '%q' "$REPO") && GSD_PROJECT=999 bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 2 ]
  [[ "$output" == *"GSD_PROJECT=999 but .planning/999/ROADMAP.md is missing"* ]]
}

@test "GSD_PROJECT with a path separator or traversal is refused" {
  write_roadmap
  write_plan 01 '[FR-001, FR-002, FR-003]'

  run bash -c "cd $(printf '%q' "$REPO") && GSD_PROJECT=../x bash $(printf '%q' "$SCRIPT") 2"

  [ "$status" -eq 2 ]
  [[ "$output" == *"GSD_PROJECT must be one path segment"* ]]
}

@test "a scoped project with no matching phase directory names the scoped path" {
  write_project 431
  cat >> "$REPO/.planning/431/ROADMAP.md" <<'ROADMAP'

### Phase 3: Unplanned
**Requirements**: FR-201
ROADMAP

  run bash -c "cd $(printf '%q' "$REPO") && GSD_PROJECT=431 bash $(printf '%q' "$SCRIPT") 3"

  [ "$status" -eq 2 ]
  [[ "$output" == *"no .planning/431/phases/03-* directory"* ]]
}
