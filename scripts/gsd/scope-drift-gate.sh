#!/usr/bin/env bash
# scope-drift-gate.sh — phase-boundary spec/plan drift check (ADVISORY).
#
# Long agentic runs drift: the executor tunnels on a sub-problem and the
# diff walks away from what the plan declared. The wrong fix is a per-turn
# hook (per-action gates measurably drag every edit); the right frequency
# is the phase boundary — this lever runs once per phase wall, costs one
# `git diff` in deterministic mode, and at most ONE bounded LLM call with
# `--judge`.
#
# Deterministic mode (default, zero LLM):
#   - classify `git diff --name-only <base>...HEAD` against the union of
#     `files_modified:` globs from the given PLAN.md file(s)
#     (.planning/ and spike-results/ excluded from classification)
#   - DRIFT WARNING when undeclared files exceed GSD_DRIFT_THRESHOLD_PCT
#     (default 20%) of the classified diff
#   - always re-anchors: prints `PHASE GOAL: <goal>` back into context
# Judge mode (--judge): adds one bounded cross-vendor verdict via
#   adversary-host (producer≠reviewer), `DRIFT-VERDICT: ON-TRACK|DRIFT: …`.
#   Test seam / override: GSD_DRIFT_JUDGE_CMD (prompt on stdin).
#
# Advisory contract: ALWAYS exits 0. Kill-switch: GSD_DRIFT_GATE=off.
# Usage: scope-drift-gate.sh [--base <ref>] --plan <PLAN.md> [--plan ...]
#                            [--judge] [--goal "<text>"]
set -uo pipefail

note() { echo "[scope-drift-gate] $*"; }
warn() { echo "[scope-drift-gate] WARN: $*" >&2; }

if [ "${GSD_DRIFT_GATE:-on}" = "off" ]; then
  note "disabled via GSD_DRIFT_GATE=off — skipping"
  exit 0
fi

BASE="origin/main"
JUDGE=0
GOAL=""
PLANS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --base)  BASE="$2"; shift 2 ;;
    --plan)  PLANS+=("$2"); shift 2 ;;
    --judge) JUDGE=1; shift ;;
    --goal)  GOAL="$2"; shift 2 ;;
    *) warn "unknown arg '$1' — ignoring"; shift ;;
  esac
done

# --- declared surface + goal from PLAN.md frontmatter -----------------------
DECLARED=()
for plan in ${PLANS[@]+"${PLANS[@]}"}; do
  [ -f "$plan" ] || { warn "plan file not found: $plan"; continue; }
  while IFS= read -r entry; do
    [ -n "$entry" ] && DECLARED+=("$entry")
  done < <(awk '/^---[[:space:]]*$/{f++;next} f==1' "$plan" | awk '
    /^files_modified:/ {inlist=1; next}
    inlist && /^[[:space:]]*-[[:space:]]/ {
      sub(/^[[:space:]]*-[[:space:]]*/, ""); gsub(/["'\'']/, ""); print; next }
    inlist && !/^[[:space:]]*-/ {inlist=0}')
  if [ -z "$GOAL" ]; then
    GOAL="$(awk '/^---[[:space:]]*$/{f++;next} f==1' "$plan" \
      | awk '/^goal:/{sub(/^goal:[[:space:]]*/,""); print; exit}')"
  fi
done

[ -n "$GOAL" ] && note "PHASE GOAL: $GOAL"

if [ "${#DECLARED[@]}" -eq 0 ]; then
  warn "no declared surface (no files_modified in plan args) — nothing to classify (fail-open)"
  exit 0
fi

# --- deterministic classification -------------------------------------------
CHANGED=()
while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$f" in .planning/*|spike-results/*) continue ;; esac
  CHANGED+=("$f")
done < <(git diff --name-only "$BASE"...HEAD 2>/dev/null)

UNDECLARED=()
for f in ${CHANGED[@]+"${CHANGED[@]}"}; do
  hit=0
  for p in "${DECLARED[@]}"; do
    # shellcheck disable=SC2254  # unquoted on purpose: glob match
    case "$f" in $p) hit=1; break ;; esac
  done
  [ "$hit" -eq 0 ] && UNDECLARED+=("$f")
done

TOTAL="${#CHANGED[@]}"
UND="${#UNDECLARED[@]}"
THRESHOLD="${GSD_DRIFT_THRESHOLD_PCT:-20}"
if [ "$TOTAL" -gt 0 ] && [ $(( UND * 100 )) -gt $(( TOTAL * THRESHOLD )) ]; then
  warn "DRIFT WARNING: $UND of $TOTAL changed files are OUTSIDE the declared plan surface (> ${THRESHOLD}%):"
  shown=0
  for f in "${UNDECLARED[@]}"; do
    warn "  undeclared: $f"
    shown=$(( shown + 1 ))
    if [ "$shown" -ge 20 ] && [ "$UND" -gt 20 ]; then
      warn "  … and $(( UND - 20 )) more undeclared files"
      break
    fi
  done
  warn "re-read the plan before continuing — advisory only, not blocking"
else
  note "scope OK: $UND of $TOTAL changed files undeclared (threshold ${THRESHOLD}%)"
fi

# --- optional single bounded judge call -------------------------------------
if [ "$JUDGE" -eq 1 ]; then
  DIFFSTAT="$(git diff --stat "$BASE"...HEAD 2>/dev/null | tail -40)"
  PROMPT="You are a drift auditor for an autonomous coding run. Phase goal:
${GOAL:-<no goal declared — judge from the declared surface>}

Declared file surface:
$(printf ' - %s\n' "${DECLARED[@]}")

Actual diffstat (base ${BASE}):
${DIFFSTAT}

Undeclared changed files:
$(printf ' - %s\n' ${UNDECLARED[@]+"${UNDECLARED[@]}"})

Does this work still serve the phase goal, or has the run drifted?
End with exactly one line: DRIFT-VERDICT: ON-TRACK  or  DRIFT-VERDICT: DRIFT — <one-line reason>"

  JUDGE_OUT=""
  if [ -n "${GSD_DRIFT_JUDGE_CMD:-}" ]; then
    JUDGE_OUT="$(printf '%s' "$PROMPT" | "$GSD_DRIFT_JUDGE_CMD" 2>/dev/null)" || JUDGE_OUT=""
  else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$SCRIPT_DIR/adversary-host.sh" ]; then
      # shellcheck disable=SC1091
      . "$SCRIPT_DIR/adversary-host.sh"
      ACTIVE_HOST="$(detect_orchestrator_host)" || ACTIVE_HOST="claude"
      KIND="$(adversary_kind_for_host "$ACTIVE_HOST")"
      if [ "$KIND" = "codex" ]; then PM="gpt-5.6-sol"; PE="xhigh"; else PM="opus"; PE=""; fi
      if [ "$ACTIVE_HOST" = "codex" ]; then FM="gpt-5.6-sol"; FE="xhigh"; else FM="opus"; FE=""; fi
      JUDGE_OUT="$(adversary_invoke_with_fallback "$KIND" "$ACTIVE_HOST" \
        "${GSD_DRIFT_JUDGE_TIMEOUT:-300}" "$PM" "$PE" "$FM" "$FE" "$PROMPT" 2>/dev/null)" || JUDGE_OUT=""
    fi
  fi

  VERDICT_LINE="$(printf '%s\n' "$JUDGE_OUT" | grep -E '^DRIFT-VERDICT: (ON-TRACK|DRIFT)' | tail -1 || true)"
  if [ -z "$VERDICT_LINE" ]; then
    warn "judge unavailable or returned no anchored verdict — continuing (fail-soft)"
  else
    note "$VERDICT_LINE"
    case "$VERDICT_LINE" in
      *DRIFT-VERDICT:\ DRIFT*)
        warn "judge flagged drift (advisory) — re-read spec/plan success criteria before the next phase" ;;
    esac
  fi
fi

exit 0
