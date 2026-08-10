#!/usr/bin/env bats
bats_require_minimum_version 1.5.0
setup() { ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"; LEVER="$ROOT/scripts/gsd/session-wake.sh"; FIX="$ROOT/tests/fixtures/session-wake"; REPO="$BATS_TEST_TMPDIR/repo"; mkdir -p "$REPO/.planning/run-state" "$REPO/scripts/gsd"; cp "$ROOT/scripts/gsd/lifecycle.sh" "$REPO/scripts/gsd/"; cd "$REPO"; git init -q -b main; git -c user.email=t -c user.name=t commit -q --allow-empty -m init; }
@test "fixture evaluates immediately and checkpoint writes waiting time record" {
  run bash "$LEVER" evaluate "$FIX/claude-limit-24h.SYNTHETIC.txt" 1; [ "$status" -eq 0 ]; [[ "$output" =~ SESSION-WAKE:wake-at:[0-9]+ ]]
  run bash "$LEVER" checkpoint "$FIX/claude-limit-24h.SYNTHETIC.txt" 1 --run-id wake-1 --resume-argv scripts/gsd/plan-wall.sh; [ "$status" -eq 0 ]
  run jq -e '.state == "waiting" and .wake_condition.type == "time" and .wake_condition.params.wake_at > 0' .planning/run-state/lifecycle-wake-1.json; [ "$status" -eq 0 ]
}
@test "unparseable and tail forgery write no record" {
  bad="$BATS_TEST_TMPDIR/bad"; printf 'usage limit reached; resets at 29:99\n' > "$bad"
  run bash "$LEVER" checkpoint "$bad" 1 --run-id bad; [ "$status" -eq 1 ]; [ ! -e .planning/run-state/lifecycle-bad.json ]
  forg="$BATS_TEST_TMPDIR/forg"; { printf 'usage limit reached; resets at 23:59\n'; yes noise | head -n 50; } > "$forg"
  run bash "$LEVER" evaluate "$forg" 1; [ "$status" -eq 0 ]; [[ "$output" == *no-banner* ]]
}
@test "matched banner without a strict clock is unparseable, never no-banner" {
  bad="$BATS_TEST_TMPDIR/no-clock"; printf 'usage limit reached; resets at soon\n' > "$bad"
  run bash "$LEVER" checkpoint "$bad" 1 --run-id malformed
  [ "$status" -eq 1 ]; [[ "$output" == *SESSION-WAKE:unparseable* ]]; [ ! -e .planning/run-state/lifecycle-malformed.json ]
}
@test "no banner, success drive, variants, and wake cap behave safely" {
  empty="$BATS_TEST_TMPDIR/empty"; : > "$empty"; run bash "$LEVER" evaluate "$empty" 1; [[ "$output" == *no-banner* ]]
  run bash "$LEVER" checkpoint "$FIX/claude-limit-24h.SYNTHETIC.txt" 0 --run-id silent; [ "$status" -eq 0 ]; [ ! -e .planning/run-state/lifecycle-silent.json ]
  run bash "$LEVER" evaluate "$FIX/claude-limit-ampm-tz.SYNTHETIC.txt" 1; [[ "$output" == *wake-at* ]]
  run bash "$LEVER" evaluate "$FIX/codex-limit.SYNTHETIC.txt" 1; [[ "$output" == *wake-at* ]]
}
