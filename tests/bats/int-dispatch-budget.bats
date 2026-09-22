#!/usr/bin/env bats
# int-dispatch-budget.bats — bounded continuation policy plus the preserved
# legacy grant-ledger syntax. New managed runs use ControlStore's durable
# cumulative limits; a compatibility grant cannot replenish those limits.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  GATES="$ROOT/lib/gates.py"
  export GATES_STORE="$BATS_TEST_TMPDIR/evidence.json"
}

@test "feature-implement pins cumulative limits and bans identity-based resets" {
  cd "$ROOT"
  grep -q 'fixed on first use' skills/feature-implement/SKILL.md
  grep -q 'not replenished by generic reset' skills/feature-implement/SKILL.md
  grep -q 'Do not edit a plan, rename a phase, or mint an adhoc ID' skills/feature-implement/SKILL.md
}

@test "gates.py grant/check-grant round-trips the typed dispatch-budget action" {
  run -0 python3 "$GATES" grant run-x --action "dispatch-budget:381:6" --rollback "revoke grant"
  run -0 python3 "$GATES" check-grant run-x --action "dispatch-budget:381:6"
  # ungranted phase stays refused
  run python3 "$GATES" check-grant run-x --action "dispatch-budget:381:7"
  [ "$status" -ne 0 ]
}
