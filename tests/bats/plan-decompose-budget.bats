#!/usr/bin/env bats
# plan-decompose durable round budget (D2, 2026-08-27): the plan-gate review
# budget is charged on the REAL gates.py loop-round counter under the stable
# `spec-$SPEC_ID` run id, so a re-invocation after a terminal block re-emits
# the block with zero dispatch instead of minting a fresh budget (spec-388
# rounds 4-6 pathology). Prose pins + a live counter check against a scratch
# store — the skill is prose, so the counter mechanics are proven directly.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  GATES="$ROOT/lib/gates.py"
  SKILL="$ROOT/skills/plan-decompose/SKILL.md"
  export GATES_STORE="$BATS_TEST_TMPDIR/evidence.json"
}

@test "budget mechanics: 1+PLAN_GATE_MAX_REPAIRS rounds, then LOOP-CAP, then --reset restores round 1" {
  # default budget: 1 review + 1 repair = max 2
  run -0 python3 "$GATES" loop-round "spec-999" "plangate:plan" --max 2
  [[ "$output" == *"round 1/2"* ]]
  run -0 python3 "$GATES" loop-round "spec-999" "plangate:plan" --max 2
  [[ "$output" == *"round 2/2"* ]]
  # third invocation: budget spent — the skill must re-emit the terminal
  # block WITHOUT dispatching a reviewer
  run -1 python3 "$GATES" loop-round "spec-999" "plangate:plan" --max 2
  [[ "$output" == *"LOOP-CAP"* ]]
  # explicit operator reset is the ONLY fresh-budget lever
  run -0 python3 "$GATES" loop-round "spec-999" "plangate:plan" --reset --max 1
  run -0 python3 "$GATES" loop-round "spec-999" "plangate:plan" --max 2
  [[ "$output" == *"round 1/2"* ]]
}

@test "counter is per-spec: another spec id starts fresh" {
  run -0 python3 "$GATES" loop-round "spec-111" "plangate:plan" --max 2
  run -0 python3 "$GATES" loop-round "spec-111" "plangate:plan" --max 2
  run -1 python3 "$GATES" loop-round "spec-111" "plangate:plan" --max 2
  run -0 python3 "$GATES" loop-round "spec-222" "plangate:plan" --max 2
  [[ "$output" == *"round 1/2"* ]]
}

@test "unusable store is nonzero-but-not-cap (skill fails open, never fake-caps)" {
  : > "$GATES_STORE"
  chmod 444 "$GATES_STORE"
  run python3 "$GATES" loop-round "spec-999" "plangate:plan" --max 2
  chmod 644 "$GATES_STORE"
  [ "$status" -ne 0 ]
  [ "$status" -ne 1 ]
  [[ "$output" == *"LOOP-ROUND-ERROR"* ]]
}

# ── prose pins ─────────────────────────────────────────────────────────────

@test "SKILL pins the stable spec-derived run id and the durable findings file" {
  grep -q 'loop-round "spec-\$SPEC_ID" "plangate:plan"' "$SKILL"
  grep -q 'never a session-derived id' "$SKILL"
  grep -q 'plan-review-findings.json' "$SKILL"
  grep -q "deliberately SURVIVES budget" "$SKILL"
}

@test "SKILL pins pass-reset and the explicit fresh-budget recipe" {
  grep -q 'plangate:plan --reset' "$SKILL"
  grep -qE 'On a final PASS verdict.*' "$SKILL"
  grep -q 'plangate:plan --reset --max 1' "$SKILL"
}

@test "SKILL pins the one-round pass rule (no strictly-fewer comparison) and default 1 repair" {
  grep -q 'PASS\*\* immediately, round 1 included' "$SKILL"
  grep -q 'There is NO round-count comparison' "$SKILL"
  grep -q 'PLAN_GATE_MAX_REPAIRS:-1' "$SKILL"
  ! grep -q 'STRICTLY FEWER' "$SKILL"
}
