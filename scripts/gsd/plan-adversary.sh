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
# PASSES ($2): the adversary runs ONCE per plan regardless of PASSES —
# idempotent-by-design. The "already reviewed" check below makes extra passes
# deliberate no-ops, so gsd's plan_bounce_passes>1 re-bounces never duplicate
# review sections. Accepted for seam-contract compatibility; not consumed.
#
# Cost guard: only high-blast plans (auth/RLS/payments/migrations/…) burn the
# xhigh review; everything else no-ops. Kill-switch: PLAN_ADVERSARY=off.
#
# Host-aware: the adversary is always the OPPOSITE vendor CLI from whichever
# harness is orchestrating (claude vs codex) — see adversary-host.sh.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091 # dynamic path, resolved at runtime via BASH_SOURCE
source "$SCRIPT_DIR/adversary-host.sh"

PLAN_FILE="${1:-}"
# shellcheck disable=SC2034 # accepted for the gsd bounce seam; see PASSES note above
PASSES="${2:-1}"
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

# When fable is down AND cross-vendor (sol) compensation is live (model-fallback.sh
# marker mode=codex-sol), every plan is bounce-worthy — the xhigh sol pass IS the
# compensation for the missing fable planning-tier review, so low-blast plans can't
# skip it here.
FABLE_MARKER="${PLAN_ADVERSARY_FABLE_MARKER:-.planning/fable-fallback.json}"
LOW_BLAST_SKIP_DISABLED=false
if [ -f "$FABLE_MARKER" ] && grep -q '"mode"[[:space:]]*:[[:space:]]*"codex-sol"' "$FABLE_MARKER"; then
  LOW_BLAST_SKIP_DISABLED=true
  echo "[plan-adversary] fable-fallback marker — low-blast skip disabled" >&2
fi

if [ "$LOW_BLAST_SKIP_DISABLED" = false ] && ! grep -Eiq "$KEYWORDS" "$PLAN_FILE"; then
  echo "[plan-adversary] low-blast plan — skipped"
  exit 0
fi

# Idempotent across bounce passes and plan-phase re-runs.
if grep -q '^## Adversarial plan review' "$PLAN_FILE"; then
  echo "[plan-adversary] already reviewed — skipped"
  exit 0
fi

ACTIVE_HOST="$(detect_orchestrator_host)"
ADVERSARY_KIND="${PLAN_ADVERSARY_KIND:-$(adversary_kind_for_host "$ACTIVE_HOST")}"
FALLBACK_KIND="$(adversary_kind_for_host "$ADVERSARY_KIND")"

if [ "$ADVERSARY_KIND" = "codex" ]; then
  ADVERSARY_BIN_CODEX="${PLAN_ADVERSARY_BIN:-${ADVERSARY_BIN_CODEX:-codex}}"
  MODEL="${PLAN_ADVERSARY_MODEL:-gpt-5.6-sol}"
  EFFORT="${PLAN_ADVERSARY_EFFORT:-xhigh}"
  HEADER_LABEL="${MODEL} ${EFFORT}"
else
  ADVERSARY_BIN_CLAUDE="${PLAN_ADVERSARY_BIN:-${ADVERSARY_BIN_CLAUDE:-claude}}"
  MODEL="${PLAN_ADVERSARY_CLAUDE_MODEL:-opus}"
  EFFORT=""
  HEADER_LABEL="claude ${MODEL}"
fi
if [ "$FALLBACK_KIND" = "codex" ]; then
  ADVERSARY_BIN_CODEX="${PLAN_ADVERSARY_FALLBACK_BIN:-${ADVERSARY_BIN_CODEX:-codex}}"
  FALLBACK_MODEL="${PLAN_ADVERSARY_MODEL:-gpt-5.6-sol}"
  FALLBACK_EFFORT="${PLAN_ADVERSARY_EFFORT:-xhigh}"
  FALLBACK_HEADER="${FALLBACK_MODEL} ${FALLBACK_EFFORT}, degraded fallback"
else
  ADVERSARY_BIN_CLAUDE="${PLAN_ADVERSARY_FALLBACK_BIN:-${ADVERSARY_BIN_CLAUDE:-claude}}"
  FALLBACK_MODEL="${PLAN_ADVERSARY_CLAUDE_MODEL:-opus}"
  FALLBACK_EFFORT=""
  FALLBACK_HEADER="claude ${FALLBACK_MODEL}, degraded fallback"
fi
export ADVERSARY_BIN_CODEX ADVERSARY_BIN_CLAUDE

PROMPT="You are a brutally honest principal engineer reviewing an execution PLAN before any code is written. Nothing is implemented yet — every finding here is 100x cheaper than the same finding in code review.

--- PLAN START ---
$(cat "$PLAN_FILE")
--- PLAN END ---

Hunt for: claims about the codebase or its APIs that could be wrong, logical gaps, unstated assumptions, missing or unfalsifiable acceptance criteria, sequencing hazards (a later task invalidating an earlier one), and security holes in the approach itself. Tag each finding on its own line starting with CRITICAL:, HIGH:, or MEDIUM:. End your response with exactly one line: VERDICT: APPROVE or VERDICT: REVISE."

OUTPUT="$(adversary_invoke_with_fallback "$ADVERSARY_KIND" "$FALLBACK_KIND" \
  "${PLAN_ADVERSARY_TIMEOUT:-480}" "$MODEL" "$EFFORT" \
  "$FALLBACK_MODEL" "$FALLBACK_EFFORT" "$PROMPT" 2>&1)"
rc=$?
if [ $rc -ne 0 ]; then
  echo "[plan-adversary] both review hosts unavailable (rc=$rc) — mandatory plan review blocked" >&2
  exit 1
fi

DEGRADED_LINE="$(printf '%s\n' "$OUTPUT" | grep -E '^adversary-host: DEGRADED ' | head -1)"
if [ -n "$DEGRADED_LINE" ]; then
  echo "[plan-adversary] $DEGRADED_LINE" >&2
  HEADER_LABEL="$FALLBACK_HEADER"
fi

# codex echoes the prompt into its transcript — only line-anchored tags count,
# and the prompt's own instruction lines never start a line with 'CRITICAL:' etc.
VERDICT_LINES="$(printf '%s\n' "$OUTPUT" | grep -E '^VERDICT: (APPROVE|REVISE)[[:space:]]*$' || true)"
VERDICT_COUNT="$(printf '%s\n' "$VERDICT_LINES" | awk 'NF { count++ } END { print count+0 }')"
if [ "$VERDICT_COUNT" -ne 1 ]; then
  echo "[plan-adversary] mandatory review returned a missing or conflicting anchored verdict; plan left unchanged" >&2
  exit 1
fi
VERDICT="$VERDICT_LINES"
FINDINGS="$(printf '%s\n' "$OUTPUT" | grep -E '^(CRITICAL|HIGH|MEDIUM):' | head -30)"
N_FINDINGS=0
[ -n "$FINDINGS" ] && N_FINDINGS="$(printf '%s\n' "$FINDINGS" | wc -l | tr -d ' ')"

{
  echo ""
  echo "## Adversarial plan review (${HEADER_LABEL})"
  echo ""
  echo "$VERDICT"
  if [ -n "$FINDINGS" ]; then
    echo ""
    printf '%s\n' "$FINDINGS"
  fi
} >> "$PLAN_FILE"

echo "[plan-adversary] reviewed: ${VERDICT} — ${N_FINDINGS} finding(s) appended to $PLAN_FILE"
exit 0
