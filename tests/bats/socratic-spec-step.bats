#!/usr/bin/env bats
# socratic-spec-step.bats — plan 02-02: fail-closed --validate mode on
# socratic-slice.sh (Task 1), the Step 1.5 producer prose in
# skills/feature-spec/SKILL.md (Task 2), and the Step 6 PENDING-not-grant
# routing plus --record-pendings injection guard (Task 3).

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/socratic-slice.sh"
  SKILL="$ROOT/skills/feature-spec/SKILL.md"
  load 'helpers/socratic-fixtures'
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  SPEC="$BATS_TEST_TMPDIR/spec"
  make_vendor_tree "$VENDOR"
}

# Writes a well-formed, passing socratic.md fixture at $SPEC/socratic.md.
make_valid_fixture() {
  local dir="$1"
  mkdir -p "$dir"
  cat > "$dir/socratic.md" <<'EOF'
<!-- valid domains: requirements, frontend, ... ; depth: core|full; packs: operations, ... -->
---
domains: [requirements, testing]
depth: core
packs: [operations]
---
## Self-answered highlights
- highlight one

## Assumed (flag if wrong)
- ASSUME-001: default taken — defensible because X

## Open questions → grants
- none

## Top risks
- risk one
EOF
}

# ---------------------------------------------------------------------------
# Task 1: fail-closed --validate mode
# ---------------------------------------------------------------------------

@test "validate passes a well-formed socratic.md" {
  make_valid_fixture "$SPEC"

  run bash "$SCRIPT" --validate "$SPEC/socratic.md"

  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "validate reuses the shared anchored status pattern" {
  make_valid_fixture "$SPEC"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1 >/dev/null"
  [ "$status" -eq 0 ]
  count="$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')"
  [ "$count" -eq 1 ]
  [[ "$output" == *"(validate)"* ]]

  make_spec_dir "$SPEC" "domains: [typo-domain]
depth: core
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1 >/dev/null"
  [ "$status" -eq 3 ]
  count="$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')"
  [ "$count" -eq 1 ]
  [[ "$output" == *"validate-failed:"* ]]

  make_spec_dir "$SPEC" "domains: [requirements]
depth: core
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1 >/dev/null"
  [ "$status" -eq 0 ]
  [[ "$output" == *"packs=none"* ]]

  make_spec_dir "$SPEC" "domains: [requirements]
depth: core
packs: [operations, threat-modeling, software-design]" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1 >/dev/null"
  [ "$status" -eq 0 ]
  [[ "$output" == *"packs=operations,threat-modeling,software-design"* ]]
}

@test "validate rejects an unknown domain slug" {
  make_spec_dir "$SPEC" "domains: [requirements, typo-domain]
depth: core
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"

  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"typo-domain"* ]]
}

@test "validate rejects an unknown pack name and an out-of-enum depth" {
  make_spec_dir "$SPEC" "domains: [requirements]
depth: core
packs: [typo-pack]" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"typo-pack"* ]]

  make_spec_dir "$SPEC" "domains: [requirements]
depth: bogus
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"bogus"* ]]

  # Contrast: the SAME depth=bogus fixture over the non-validate path warns
  # and falls back to core, and arms on the sibling valid domain.
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"socratic: armed"* ]]
  [[ "$output" == *"domains=requirements"* ]]
}

@test "validate rejects an out-of-enum pack even beyond the EDGE-007 cap" {
  make_spec_dir "$SPEC" "domains: [requirements]
depth: core
packs: [operations, threat-modeling, software-design, typo-pack]" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"

  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"typo-pack"* ]]
}

@test "validate ignores the SOCRATIC=off kill switch" {
  make_valid_fixture "$SPEC"
  run bash -c "SOCRATIC=off bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"SOCRATIC=off"* ]]

  make_spec_dir "$SPEC" "domains: [typo-domain]
depth: core
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"
  run bash -c "SOCRATIC=off bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" != *"SOCRATIC=off"* ]]
}

@test "validate needs no vendor tree" {
  make_valid_fixture "$SPEC"
  run bash -c "FFS_SOCRATIC_DIR='$BATS_TEST_TMPDIR/does-not-exist' bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 0 ]

  make_spec_dir "$SPEC" "domains: [typo-domain]
depth: core
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"
  run bash -c "FFS_SOCRATIC_DIR='$BATS_TEST_TMPDIR/does-not-exist' bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
}

@test "validate rejects an empty domains list" {
  make_spec_dir "$SPEC" "domains: []
depth: core
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"

  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
}

@test "validate reports every offending value, not just the first" {
  make_spec_dir "$SPEC" "domains: [typo-domain]
depth: bogus
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"

  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"typo-domain"* ]]
  [[ "$output" == *"bogus"* ]]
}

@test "validate rejects a missing required section" {
  local base="domains: [requirements]
depth: core
packs: []"

  make_spec_dir "$SPEC" "$base" "## Assumed (flag if wrong)
## Open questions → grants
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"Self-answered highlights"* ]]

  make_spec_dir "$SPEC" "$base" "## Self-answered highlights
## Open questions → grants
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"Assumed"* ]]

  make_spec_dir "$SPEC" "$base" "## Self-answered highlights
## Assumed (flag if wrong)
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"Open questions"* ]]

  make_spec_dir "$SPEC" "$base" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
  [[ "$output" == *"Top risks"* ]]

  # ASCII-arrow near-miss on the typographic-arrow heading must also fail.
  make_spec_dir "$SPEC" "$base" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions -> grants
## Top risks"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
}

@test "validate rejects a missing socratic.md and malformed frontmatter" {
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]

  mkdir -p "$SPEC"
  cat > "$SPEC/socratic.md" <<'EOF'
domains: [requirements]
---
EOF
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]
}

@test "validate is fail-closed where consumption is fail-soft" {
  make_spec_dir "$SPEC" "domains: [requirements, typo-domain]
depth: core
packs: []" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"

  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 3 ]

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"socratic: armed"* ]]
  [[ "$output" == *"domains=requirements"* ]]
}

@test "validate passes SILENTLY on three or more IN-ENUM packs" {
  make_spec_dir "$SPEC" "domains: [requirements]
depth: core
packs: [operations, threat-modeling, software-design]" "## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
## Top risks"

  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" != *"WARN"* ]]
  [[ "$output" != *"software-design"* ]] || [[ "$output" == *"packs=operations,threat-modeling,software-design"* ]]
}

@test "validate never reads stdin and writes nothing to stdout" {
  make_valid_fixture "$SPEC"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' </dev/null 2>/dev/null"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "--validate combined with --mode is a usage error" {
  make_valid_fixture "$SPEC"
  run bash -c "bash '$SCRIPT' --validate '$SPEC/socratic.md' --mode plan 2>&1"
  [ "$status" -eq 2 ]
}

# ---------------------------------------------------------------------------
# Task 2: Step 1.5 producer prose in skills/feature-spec/SKILL.md
# ---------------------------------------------------------------------------

@test "Step 1.5 sits between Step 1 and Step 2 and is labelled fail-closed authoring" {
  step1_line="$(grep -n '^### Step 1 — speckit.specify' "$SKILL" | cut -d: -f1)"
  step15_line="$(grep -n '^### Step 1.5 — socratic self-interrogation' "$SKILL" | cut -d: -f1)"
  step2_line="$(grep -n '^### Step 2 — speckit.plan' "$SKILL" | cut -d: -f1)"
  [ -n "$step1_line" ]
  [ -n "$step15_line" ]
  [ -n "$step2_line" ]
  [ "$step15_line" -gt "$step1_line" ]
  [ "$step15_line" -lt "$step2_line" ]
  grep -qF '(fail-closed authoring)' "$SKILL"
  ! grep -qE '### Step 1\.5.*\(fail-soft\)' "$SKILL"
}

@test "the step declares the socratic.md contract" {
  grep -qF 'domains' "$SKILL"
  grep -qF 'depth' "$SKILL"
  grep -qF 'packs' "$SKILL"
  grep -qF '## Self-answered highlights' "$SKILL"
  grep -qF '## Assumed (flag if wrong)' "$SKILL"
  grep -qF '## Open questions → grants' "$SKILL"
  grep -qF '## Top risks' "$SKILL"
  grep -qiF 'single-line comment' "$SKILL"
}

@test "the step declares depth auto-escalation with the pin's own signals" {
  grep -qF 'authentication' "$SKILL"
  grep -qF 'money' "$SKILL"
  grep -qF 'PII' "$SKILL"
  grep -qF 'production' "$SKILL"
  grep -qF 'core' "$SKILL"
  grep -qF 'full' "$SKILL"
}

@test "the step invokes the fail-closed validator and bounds its repair attempts" {
  grep -qF -- '--validate' "$SKILL"
  grep -qiF 'two repair attempts' "$SKILL"
  grep -qiF 'exit 3' "$SKILL"
  grep -qiF 'exit 2' "$SKILL"
}

@test "persistent validation failure terminates the step loudly" {
  grep -qiF 'exits nonzero' "$SKILL"
  grep -qiF 'do NOT proceed to Step 2' "$SKILL"
  grep -qiF 'never continues unarmed' "$SKILL"
}

@test "the step states the autonomy invariant" {
  grep -qF 'AskUserQuestion' "$SKILL"
  grep -qiF 'never calls AskUserQuestion' "$SKILL"
}

@test "the step is fail-soft on an absent vendor tree" {
  grep -qiF 'no vendored socratic tree resolves' "$SKILL"
  grep -qiF 'skip the step silently' "$SKILL"
}

@test "SOCRATIC=off is the first gate" {
  socratic_off_line="$(grep -n 'SOCRATIC is set to off' "$SKILL" | cut -d: -f1)"
  vendor_gate_line="$(grep -n 'no vendored socratic tree resolves' "$SKILL" | cut -d: -f1)"
  [ -n "$socratic_off_line" ]
  [ -n "$vendor_gate_line" ]
  [ "$socratic_off_line" -lt "$vendor_gate_line" ]
  grep -qiF 'no socratic.md is authored' "$SKILL"
}

@test "the validator is resolved through a candidate ladder" {
  grep -qF 'packages/feature-fix-swarm/lib' "$SKILL"
  ! grep -qE '^\s*bash scripts/gsd/socratic-slice\.sh' "$SKILL"
}

@test "every exit code has a stated disposition" {
  grep -qiF '126' "$SKILL"
  grep -qiF '127' "$SKILL"
  grep -qiF 'helper-unavailable' "$SKILL"
  grep -qiF 'environment error' "$SKILL"
  grep -qiF 'only exit 3' "$SKILL"
}

@test "the helper-unavailable path renames the artifact out of the way" {
  grep -qF 'socratic.md.unvalidated' "$SKILL"
  grep -qiF 'resolve socratic.md' "$SKILL"
}

@test "the pipeline diagram advertises the new phase" {
  grep -qiF 'Phase 1.5' "$SKILL"
}

@test "the SKILL.md edit keeps the host-dispatch contract heading" {
  grep -qF '## Host dispatch contract' "$SKILL"
}

# ---------------------------------------------------------------------------
# Task 3: Step 6 PENDING routing + --record-pendings injection guard
# ---------------------------------------------------------------------------

@test "Step 6 walks socratic.md alongside tasks.md and plan.md" {
  step6_line="$(grep -n '^### Step 6 — autonomy grant' "$SKILL" | cut -d: -f1)"
  summary_line="$(grep -n '^### Completion summary' "$SKILL" | cut -d: -f1)"
  walk_line="$(grep -n 'socratic\.md' "$SKILL" | awk -F: -v lo="$step6_line" -v hi="$summary_line" '$1>lo && $1<hi {print $1; exit}')"
  [ -n "$step6_line" ]
  [ -n "$summary_line" ]
  [ -n "$walk_line" ]
}

@test "socratic-sourced actions are recorded PENDING, never granted" {
  step6_line="$(grep -n '^### Step 6 — autonomy grant' "$SKILL" | cut -d: -f1)"
  summary_line="$(grep -n '^### Completion summary' "$SKILL" | cut -d: -f1)"
  pending_line="$(grep -n 'gates.py.*pending\|GATES_PY.*pending' "$SKILL" | awk -F: -v lo="$step6_line" -v hi="$summary_line" '$1>lo && $1<hi {print $1; exit}')"
  [ -n "$pending_line" ]
  grep -qiF 'never enter the auto-grant enumeration' "$SKILL"

  enum_line="$(grep -n 'Walk tasks.md + plan.md and enumerate' "$SKILL" | cut -d: -f1)"
  [ -n "$enum_line" ]
  [[ "$(sed -n "${enum_line}p" "$SKILL")" != *"socratic.md"* ]]
}

@test "Step 6 calls the helper rather than hand-rolling the shell" {
  grep -qF -- '--record-pendings' "$SKILL"
  step6_line="$(grep -n '^### Step 6 — autonomy grant' "$SKILL" | cut -d: -f1)"
  summary_line="$(grep -n '^### Completion summary' "$SKILL" | cut -d: -f1)"
  record_line="$(grep -n -- '--record-pendings' "$SKILL" | awk -F: -v lo="$step6_line" -v hi="$summary_line" '$1>lo && $1<hi {print $1; exit}')"
  [ -n "$record_line" ]
}

@test "the action string is regex-gated before interpolation" {
  grep -qF '^[a-z-]+:[A-Za-z0-9._/@-]+$' "$SCRIPT"
  grep -qF 'review:malformed-socratic-entry' "$SCRIPT"
  grep -qiF 'single-quot' "$SCRIPT"
  grep -qiF '200' "$SCRIPT"
}

@test "a metacharacter payload cannot execute" {
  local canary="$BATS_TEST_TMPDIR/canary-marker"
  rm -f "$canary"
  mkdir -p "$SPEC"
  cat > "$SPEC/socratic.md" <<EOF
---
domains: [requirements]
depth: core
packs: []
---
## Self-answered highlights
## Assumed (flag if wrong)
## Open questions → grants
- rotate:secret\$(touch $canary)'; echo pwned
## Top risks
EOF
  export GATES_STORE="$BATS_TEST_TMPDIR/evidence.json"
  run bash "$SCRIPT" --record-pendings "$SPEC/socratic.md" "spec-999"
  [ "$status" -eq 0 ]
  [ ! -e "$canary" ]

  run python3 "$ROOT/lib/gates.py" pending "spec-999"
  [[ "$output" == *"review:malformed-socratic-entry"* ]]
}

@test "the untrusted-input clause is present in Step 1.5 as well as Step 6" {
  step15_line="$(grep -n '^### Step 1.5 — socratic self-interrogation' "$SKILL" | cut -d: -f1)"
  step2_line="$(grep -n '^### Step 2 — speckit.plan' "$SKILL" | cut -d: -f1)"
  untrusted_line="$(grep -n -i 'untrusted' "$SKILL" | awk -F: -v lo="$step15_line" -v hi="$step2_line" '$1>lo && $1<hi {print $1; exit}')"
  [ -n "$untrusted_line" ]
  grep -qiF 'never auto-granted' "$SKILL"
}

@test "no new stop point in either mode" {
  grep -qiF 'recorded without stopping in MAX-AUTH' "$SKILL"
  grep -qiF 'existing Step-6 stop' "$SKILL"
}

@test "the two ledgers stay separate" {
  grep -qiF 'spec-time engineering defaults' "$SKILL"
  grep -qiF 'operator authorizations' "$SKILL"
}

@test "the completion summary rows are conditional on the file existing" {
  grep -qF 'specs/NNN/socratic.md' "$SKILL"
  grep -qiF 'ASSUME' "$SKILL"
  grep -qiF 'socratic: skipped' "$SKILL"
  grep -qF 'socratic.md.unvalidated' "$SKILL"
  grep -qiF 'NOT validated' "$SKILL"
}

@test "the pending mechanism actually behaves as the routing assumes" {
  export GATES_STORE="$BATS_TEST_TMPDIR/pending-evidence.json"
  run python3 "$ROOT/lib/gates.py" pending run-mech --action rotate:secret --reason "test"
  [ "$status" -eq 0 ]

  run python3 "$ROOT/lib/gates.py" pending run-mech
  [ "$status" -eq 1 ]
  [[ "$output" == *"rotate:secret"* ]]

  run python3 "$ROOT/lib/gates.py" check-grant run-mech --action rotate:secret
  [ "$status" -ne 0 ]
}

@test "the summary reports pendings recorded from socratic.md" {
  grep -qiF 'pending' "$SKILL"
  grep -qiF 'pending entries recorded' "$SKILL"
}

@test "the existing host-dispatch lint stays green after the SKILL.md edit" {
  run python3 -m pytest "$ROOT/tests/test_host_dispatch_lint.py" -q
  [ "$status" -eq 0 ]
}
