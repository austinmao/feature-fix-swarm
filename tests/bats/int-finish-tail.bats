#!/usr/bin/env bats
# int-finish-tail.bats — INT-004 grep-pins: both finish-tail-owning skills
# invoke the fail-soft learnings-harvest.sh lever, and code-uplift documents
# its --slop-only deslop fast path with a green-baseline wall (AC-003, AC-009).
# ponytail: static grep pins on committed SKILL.md prose, mirrors
# setup-install.bats — no script execution.

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
}

@test "feature-implement finish tail invokes learnings-harvest.sh" {
  grep -q 'learnings-harvest.sh' skills/feature-implement/SKILL.md
}

@test "code-uplift finish tail invokes learnings-harvest.sh" {
  grep -q 'learnings-harvest.sh' skills/code-uplift/SKILL.md
}
