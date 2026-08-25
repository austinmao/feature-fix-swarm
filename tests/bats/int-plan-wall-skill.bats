#!/usr/bin/env bats
# int-plan-wall-skill.bats — spec-004 INT-002 grep-pin: the feature-implement
# per-phase wall step names plan-wall.sh at the phase-resolution point
# (SKILL.md :140-146 region, NOT the :92-94 run-start bootstrap block, which
# precedes phase resolution — see plan.md "Wall placement").
# ponytail: static grep pins on committed SKILL.md prose, mirrors
# int-finish-tail.bats / setup-install.bats — no script execution.

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

@test "feature-implement Step 4 spec mode names plan-wall.sh after the ownership gate" {
  ownership_line="$(grep -n 'bash scripts/gsd/requirement-ownership-gate.sh "$N"' skills/feature-implement/SKILL.md | cut -d: -f1)"
  wall_line="$(grep -n 'bash scripts/gsd/plan-wall.sh "$PHASE_DIR"' skills/feature-implement/SKILL.md | cut -d: -f1)"
  dry_run_line="$(grep -n -- '--dry-run.*print phase N' skills/feature-implement/SKILL.md | cut -d: -f1)"
  [ -n "$ownership_line" ]
  [ -n "$wall_line" ]
  [ -n "$dry_run_line" ]
  [ "$wall_line" -gt "$ownership_line" ]
  [ "$wall_line" -lt "$dry_run_line" ]
}

@test "feature-implement plan-wall step is NOT at the run-start bootstrap block" {
  bootstrap_line="$(grep -n 'model-fallback.sh .planning' skills/feature-implement/SKILL.md | cut -d: -f1)"
  wall_line="$(grep -n 'bash scripts/gsd/plan-wall.sh "$PHASE_DIR"' skills/feature-implement/SKILL.md | cut -d: -f1)"
  [ -n "$bootstrap_line" ]
  [ -n "$wall_line" ]
  [ "$wall_line" -gt "$bootstrap_line" ]
}
