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
