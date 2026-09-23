#!/usr/bin/env bats
# int-dispatch-budget.bats — D10 (2026-08-27) grep-pins + live grant-ledger
# check: an exhausted dispatch budget is refreshed by a typed
# dispatch-budget:<spec>:<phase> grant (gates.py autonomy ledger), never by
# plan-amendment-as-budget-reset (spec-381 burned two rounds on it).

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  GATES="$ROOT/lib/gates.py"
  export GATES_STORE="$BATS_TEST_TMPDIR/evidence.json"
}

@test "feature-implement pins the typed dispatch-budget grant and bans plan-amendment resets" {
  cd "$ROOT"
  grep -q 'dispatch-budget:<spec>:<phase>' skills/feature-implement/SKILL.md
  grep -q 'NEVER amend a plan to reset a budget' skills/feature-implement/SKILL.md
  grep -q 'defect-scoped, not round-scoped' skills/feature-implement/SKILL.md
}

@test "gates.py grant/check-grant round-trips the typed dispatch-budget action" {
  run -0 python3 "$GATES" grant run-x --action "dispatch-budget:381:6" --rollback "revoke grant"
  run -0 python3 "$GATES" check-grant run-x --action "dispatch-budget:381:6"
  # ungranted phase stays refused
  run python3 "$GATES" check-grant run-x --action "dispatch-budget:381:7"
  [ "$status" -ne 0 ]
}
