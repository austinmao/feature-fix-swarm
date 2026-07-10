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
  # pin the actual invocation command, not a bare substring a comment could satisfy
  grep -qF 'bash scripts/gsd/learnings-harvest.sh' skills/feature-implement/SKILL.md
}

@test "code-uplift finish tail invokes learnings-harvest.sh" {
  # code-uplift phrases it as "`scripts/gsd/learnings-harvest.sh` learnings step"
  grep -qF 'scripts/gsd/learnings-harvest.sh` learnings step' skills/code-uplift/SKILL.md
}

@test "code-uplift documents the --slop-only fast path" {
  grep -q -- '--slop-only' skills/code-uplift/SKILL.md
}

@test "code-uplift --slop-only documents the green-baseline refusal wall" {
  grep -q 'REFUSE' skills/code-uplift/SKILL.md
}
