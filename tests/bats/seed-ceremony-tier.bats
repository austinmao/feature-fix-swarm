#!/usr/bin/env bats
# seed-ceremony-tier.sh — seed-time ceremony classifier (D3). Advisory:
# always exit 0; the printed "<tier> <reason>" line is the contract.

bats_require_minimum_version 1.5.0

setup() {
  LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/seed-ceremony-tier.sh"
  cd "$BATS_TEST_TMPDIR"
  echo "Build a plain widget that formats dates" > spec.md
  echo "Plan: two files, small helpers" > plan.md
}

@test "security keyword in spec -> full security-surface" {
  echo "Handle oauth token refresh for the widget" >> spec.md
  run bash "$LEVER" spec.md plan.md 3 100
  [ "$status" -eq 0 ]
  [ "$output" = "full security-surface" ]
}

@test "security keyword in plan (not spec) -> full security-surface" {
  echo "store the credential in the keychain" >> plan.md
  run bash "$LEVER" spec.md plan.md 3 100
  [ "$status" -eq 0 ]
  [ "$output" = "full security-surface" ]
}

@test "files>20 -> full size" {
  run bash "$LEVER" spec.md plan.md 21 500
  [ "$output" = "full size" ]
}

@test "est-LOC>1500 -> full size" {
  run bash "$LEVER" spec.md plan.md 8 1600
  [ "$output" = "full size" ]
}

@test "files<5 and est-LOC<200 -> adhoc small" {
  run bash "$LEVER" spec.md plan.md 4 199
  [ "$output" = "adhoc small" ]
}

@test "moderate size -> light default" {
  run bash "$LEVER" spec.md plan.md 8 800
  [ "$output" = "light default" ]
}

@test "unknown estimates never reach the adhoc rung -> light" {
  run bash "$LEVER" spec.md plan.md
  [ "$output" = "light default" ]
  run bash "$LEVER" spec.md plan.md banana banana
  [ "$output" = "light default" ]
}

@test "FFS_CEREMONY_TIER hard override wins over everything" {
  echo "oauth payment secret" >> spec.md
  FFS_CEREMONY_TIER=light run bash "$LEVER" spec.md plan.md 100 9999
  [ "$output" = "light override FFS_CEREMONY_TIER" ]
  FFS_CEREMONY_TIER=adhoc run bash "$LEVER" spec.md plan.md 100 9999
  [ "$output" = "adhoc override FFS_CEREMONY_TIER" ]
}

@test "garbage FFS_CEREMONY_TIER value is ignored, classifier runs" {
  FFS_CEREMONY_TIER=banana run bash "$LEVER" spec.md plan.md 8 800
  [ "$output" = "light default" ]
}

@test "missing files are tolerated (advisory: exit 0, light)" {
  run bash "$LEVER" nonexistent.md alsonot.md 8 800
  [ "$status" -eq 0 ]
  [ "$output" = "light default" ]
}

# ── prose pins (spec-decompose / feature-implement wiring) ─────────────────

@test "spec-decompose prose pins adhoc lane: interactive STOP, autonomous CONTINUE as light" {
  cd "$(dirname "$LEVER")/../.."
  grep -q 'Interactive session: STOP' skills/spec-decompose/SKILL.md
  grep -qE 'Autonomous/headless.*' skills/spec-decompose/SKILL.md
  grep -q 'CONTINUE as `light`' skills/spec-decompose/SKILL.md
  grep -q 'seed-ceremony-tier.sh' skills/spec-decompose/SKILL.md
  grep -q '.planning/ceremony-tier' skills/spec-decompose/SKILL.md
}

@test "feature-implement prose pins the run-level wall before the phase loop on tier light" {
  cd "$(dirname "$LEVER")/../.."
  grep -q 'plan-wall.sh --run' skills/feature-implement/SKILL.md
  tier_line="$(grep -n 'ceremony-tier' skills/feature-implement/SKILL.md | head -1 | cut -d: -f1)"
  ownership_line="$(grep -n 'bash scripts/gsd/requirement-ownership-gate.sh "$N"' skills/feature-implement/SKILL.md | cut -d: -f1)"
  [ -n "$tier_line" ] && [ -n "$ownership_line" ]
  [ "$tier_line" -lt "$ownership_line" ]
}
