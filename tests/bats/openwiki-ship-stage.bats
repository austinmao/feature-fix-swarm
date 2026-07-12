#!/usr/bin/env bats
# openwiki-ship-stage.bats — drop-guard for the finish-tail OpenWiki stage.
#
# The v3.21.0 ship-stage block was silently dropped in the v4.0 finish-tail
# rewrite and stayed missing through v4.5 (openclaw handoff
# 2026-07-12-ffs-openwiki-ship-tail-handoff.md). These tests make the marker
# grep-detectable so a future finish-tail rewrite that drops it fails CI.

SKILL="$BATS_TEST_DIRNAME/../../skills/feature-implement/SKILL.md"

@test "OWS-001: ship-stage markers present in feature-implement finish tail" {
  grep -q 'openwiki-wiring:ship-stage:begin' "$SKILL"
  grep -q 'openwiki-wiring:ship-stage:end' "$SKILL"
}

@test "OWS-002: staging block is warn+continue and stages openwiki/" {
  block=$(sed -n '/openwiki-wiring:ship-stage:begin/,/openwiki-wiring:ship-stage:end/p' "$SKILL")
  echo "$block" | grep -q 'git add "$ROOT/openwiki"'
  echo "$block" | grep -q 'exit 0'
  # never blocks ship: no non-zero exit in the block
  ! echo "$block" | grep -qE 'exit [1-9]'
}

@test "OWS-003: stage runs BEFORE review-gate/ship in the tail ordering" {
  # marker line number must precede the review-gate → ship line
  m=$(grep -n 'openwiki-wiring:ship-stage:begin' "$SKILL" | cut -d: -f1)
  s=$(grep -nE '^[0-9]+\. `/review-gate` → ship \(grant-walled\)' "$SKILL" | head -1 | cut -d: -f1)
  [ "$m" -lt "$s" ]
}
