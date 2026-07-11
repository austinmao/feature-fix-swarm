#!/usr/bin/env bats
# setup.sh installer manifest — static assertions that the installer ships
# every skill + gsd script the doctrine references. A skill/script that works
# in this repo but never installs into consumer repos is a silent gap.
# ponytail: grep-based static checks, not an install e2e — upgrade to a
# sandboxed-run test if the installer grows conditional logic per entry.

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

@test "setup.sh skill manifest includes code-uplift and testing-policy" {
  SKILL_LINE="$(grep '^for skill in ' setup.sh)"
  [[ "$SKILL_LINE" == *"code-uplift"* ]]
  [[ "$SKILL_LINE" == *"testing-policy"* ]]
}

@test "setup.sh gsd-script manifest includes the adversary + canary scripts" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  for s in plan-adversary.sh canary-gate.sh qa-coverage-adversary.sh adversary-host.sh; do
    [[ "$GSD_LINE" == *"gsd/$s"* ]]
  done
}

@test "setup.sh gsd-script manifest includes the v4.5.0 orchestration-hardening levers (INT-003)" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  for s in security-surface.sh review-tier.sh liveness-check.sh learnings-harvest.sh; do
    [[ "$GSD_LINE" == *"gsd/$s"* ]]
  done
}

@test "setup.sh hook manifest includes delegation-enforcer.sh (INT-003)" {
  HOOK_LINE="$(grep '^for hook in ' setup.sh)"
  [[ "$HOOK_LINE" == *"delegation-enforcer.sh"* ]]
}

@test "setup.sh gsd-script manifest includes model-equivalents.sh (spec 004 cross-vendor routing)" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  [[ "$GSD_LINE" == *"gsd/model-equivalents.sh"* ]]
}

@test "setup.sh gsd-script manifest includes model-fallback.sh + security-model-fence.sh (feature-implement Step 2 deps)" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  [[ "$GSD_LINE" == *"gsd/model-fallback.sh"* ]]
  [[ "$GSD_LINE" == *"gsd/security-model-fence.sh"* ]]
}

@test "setup.sh gsd-script manifest includes assert-merged.sh (spec 005 merge backstop)" {
  GSD_LINE="$(grep '^for gsd_script in ' setup.sh)"
  [[ "$GSD_LINE" == *"gsd/assert-merged.sh"* ]]
}

@test "setup.sh script manifest includes harness-audit.py (INT-003)" {
  SCRIPT_LINE="$(grep '^for script in ' setup.sh)"
  [[ "$SCRIPT_LINE" == *"harness-audit.py"* ]]
}
