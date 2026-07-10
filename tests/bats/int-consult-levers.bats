#!/usr/bin/env bats
# int-consult-levers.bats — grep-pins that a frozen lever is actually consulted
# from skill prose, mirroring setup-install.bats's static-assertion pattern.
# ponytail: grep-based static checks, not a full skill-run e2e — these pin the
# prose contract (INT-005, harness-audit advisory wording, INT-006), not runtime.

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

@test "adopt-wip SKILL.md consults liveness-check.sh (INT-005)" {
  grep -q 'liveness-check.sh' skills/adopt-wip/SKILL.md
}

@test "preflight SKILL.md invokes harness-audit.py" {
  grep -q 'harness-audit.py' skills/preflight/SKILL.md
}

@test "preflight harness-audit section is documented advisory, never-blocks" {
  grep -qi 'advisory' skills/preflight/SKILL.md
  grep -qi 'never block' skills/preflight/SKILL.md
}

@test "fix SKILL.md routes to /gsd-debug on non-obvious repro (INT-006)" {
  grep -q 'gsd-debug' skills/fix/SKILL.md
}

@test "fix SKILL.md documents the explicit no-failing-test AND no-single-command-repro criteria" {
  grep -qi 'no failing test' skills/fix/SKILL.md
  grep -qi 'single-command repro' skills/fix/SKILL.md
}
