#!/usr/bin/env bash
# plan-adversary.sh — gsd workflow.plan_bounce_script target: cross-model
# adversarial review of a PLAN.md BEFORE any code is written.
#
# Why plan-stage: the planner tier (fable/opus) is trusted precisely because it
# plans well — which makes an undetected plan error the most expensive kind.
# A same-family plan-checker is near-self-critique; this lever brings a
# different-provider adversary (default gpt-5.6-sol @ xhigh) to the plan.
#
# Invoked by /gsd-plan-phase's bounce step (gsd-core workflows/plan-phase.md):
#   plan-adversary.sh <PLAN_FILE> [PASSES]
# Seam contract: non-zero exit → gsd RESTORES the pre-bounce backup and
# DISCARDS the result. So findings are delivered by APPENDING a review section
# and exiting 0 — the plan-checker re-run (opus) adjudicates them. The blocking
# gate stays at ship (review-gate); this seam is advisory by design.
#
# Cost guard: only high-blast plans (auth/RLS/payments/migrations/…) burn the
# xhigh review; everything else no-ops. Kill-switch: PLAN_ADVERSARY=off.
set -uo pipefail

PLAN_FILE="${1:-}"
if [ -z "$PLAN_FILE" ] || [ ! -f "$PLAN_FILE" ]; then
  echo "[plan-adversary] usage: plan-adversary.sh <PLAN_FILE> [passes]" >&2
  exit 2
fi

if [ "${PLAN_ADVERSARY:-on}" = "off" ]; then
  echo "[plan-adversary] disabled (PLAN_ADVERSARY=off) — skipped"
  exit 0
fi

# High-blast trigger: security-model-fence keyword set + infra blast extras.
KEYWORDS="${PLAN_ADVERSARY_KEYWORDS:-auth|rls|row[ _-]?level|payment|stripe|crypto|jwt|jwks|oauth|owasp|secret|credential|password|migration|multi[ -]?tenant|deploy|provision}"
if ! grep -Eiq "$KEYWORDS" "$PLAN_FILE"; then
  echo "[plan-adversary] low-blast plan — skipped"
  exit 0
fi

# Idempotent across bounce passes and plan-phase re-runs.
if grep -q '^## Adversarial plan review' "$PLAN_FILE"; then
  echo "[plan-adversary] already reviewed — skipped"
  exit 0
fi

ADVERSARY_BIN="${PLAN_ADVERSARY_BIN:-codex}"
if ! command -v "$ADVERSARY_BIN" >/dev/null 2>&1; then
  echo "[plan-adversary] $ADVERSARY_BIN CLI not found — skipped (fail-soft)"
  exit 0
fi

MODEL="${PLAN_ADVERSARY_MODEL:-gpt-5.6-sol}"
EFFORT="${PLAN_ADVERSARY_EFFORT:-xhigh}"

PROMPT="You are a brutally honest principal engineer reviewing an execution PLAN before any code is written. Nothing is implemented yet — every finding here is 100x cheaper than the same finding in code review.

--- PLAN START ---
$(cat "$PLAN_FILE")
--- PLAN END ---

Hunt for: claims about the codebase or its APIs that could be wrong, logical gaps, unstated assumptions, missing or unfalsifiable acceptance criteria, sequencing hazards (a later task invalidating an earlier one), and security holes in the approach itself. Tag each finding on its own line starting with CRITICAL:, HIGH:, or MEDIUM:. End your response with exactly one line: VERDICT: APPROVE or VERDICT: REVISE."

# codex exec: prompt as arg, stdin MUST be /dev/null (hangs otherwise).
if command -v timeout >/dev/null 2>&1; then
  OUTPUT="$(timeout "${PLAN_ADVERSARY_TIMEOUT:-480}" "$ADVERSARY_BIN" exec \
    -c "model=\"$MODEL\"" -c "model_reasoning_effort=\"$EFFORT\"" \
    "$PROMPT" </dev/null 2>&1)"
  rc=$?
else
  OUTPUT="$("$ADVERSARY_BIN" exec \
    -c "model=\"$MODEL\"" -c "model_reasoning_effort=\"$EFFORT\"" \
    "$PROMPT" </dev/null 2>&1)"
  rc=$?
fi
if [ $rc -ne 0 ]; then
  echo "[plan-adversary] $ADVERSARY_BIN exec failed (rc=$rc) — skipped (fail-soft)"
  exit 0
fi

# codex echoes the prompt into its transcript — only line-anchored tags count,
# and the prompt's own instruction lines never start a line with 'CRITICAL:' etc.
VERDICT="$(printf '%s\n' "$OUTPUT" | grep -E '^VERDICT: (APPROVE|REVISE)[[:space:]]*$' | tail -1)"
[ -z "$VERDICT" ] && VERDICT="VERDICT: UNPARSEABLE"
FINDINGS="$(printf '%s\n' "$OUTPUT" | grep -E '^(CRITICAL|HIGH|MEDIUM):' | head -30)"
N_FINDINGS=0
[ -n "$FINDINGS" ] && N_FINDINGS="$(printf '%s\n' "$FINDINGS" | wc -l | tr -d ' ')"

{
  echo ""
  echo "## Adversarial plan review (${MODEL} ${EFFORT})"
  echo ""
  echo "$VERDICT"
  if [ -n "$FINDINGS" ]; then
    echo ""
    printf '%s\n' "$FINDINGS"
  fi
} >> "$PLAN_FILE"

echo "[plan-adversary] reviewed: ${VERDICT} — ${N_FINDINGS} finding(s) appended to $PLAN_FILE"
exit 0
