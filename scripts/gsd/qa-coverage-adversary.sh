#!/usr/bin/env bash
# qa-coverage-adversary.sh — advisory (NEVER blocks) cross-model critique of
# QA browser-step coverage vs the code diff. Same host-aware adversary lib
# as plan-adversary.sh: always the OPPOSITE vendor CLI from whichever
# harness is orchestrating. Kill-switch: QA_COVERAGE=off.
#
# Usage: qa-coverage-adversary.sh <results.json> [--diff-base <ref>]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091 # dynamic path, resolved at runtime via BASH_SOURCE
source "$SCRIPT_DIR/adversary-host.sh"

RESULTS_FILE="${1:-}"
if [ -z "$RESULTS_FILE" ]; then
  echo "[qa-coverage-adversary] usage: qa-coverage-adversary.sh <results.json> [--diff-base <ref>]" >&2
  exit 2
fi
shift || true

# Resolves through base-branch.sh; --diff-base still overrides.
DIFF_BASE="origin/$(bash "$(dirname "${BASH_SOURCE[0]:-$0}")/base-branch.sh" 2>/dev/null || echo main)"
while [ $# -gt 0 ]; do
  case "$1" in
    --diff-base)
      if [ $# -lt 2 ]; then
        echo "[qa-coverage-adversary] usage: qa-coverage-adversary.sh <results.json> [--diff-base <ref>]" >&2
        exit 2
      fi
      DIFF_BASE="$2"; shift 2 ;;
    -*)
      echo "[qa-coverage-adversary] usage: qa-coverage-adversary.sh <results.json> [--diff-base <ref>]" >&2
      exit 2
      ;;
    *) shift ;;
  esac
done

if [ "${QA_COVERAGE:-on}" = "off" ]; then
  echo "[qa-coverage-adversary] disabled (QA_COVERAGE=off) — skipped"
  exit 0
fi

if [ ! -f "$RESULTS_FILE" ]; then
  echo "[qa-coverage-adversary] results file not found — skipped (fail-soft)"
  exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "[qa-coverage-adversary] jq not found — skipped (fail-soft)"
  exit 0
fi

STEPS="$(jq -r '.steps[].name' "$RESULTS_FILE" 2>/dev/null)"
if [ -z "$STEPS" ]; then
  echo "[qa-coverage-adversary] no steps in results — skipped (fail-soft)"
  exit 0
fi
STEPS_LIST="$(printf '%s' "$STEPS" | paste -sd, -)"

DIFF_FILES="$(git diff --name-only "${DIFF_BASE}...HEAD" 2>/dev/null)"
if [ -z "$DIFF_FILES" ]; then
  DIFF_FILES="(no diff context)"
else
  DIFF_FILES="$(printf '%s' "$DIFF_FILES" | paste -sd, -)"
fi

PROMPT="QA ran these browser steps: ${STEPS_LIST}. The diff touches: ${DIFF_FILES}. List user-facing flows the QA MISSED, one per line starting \`MISSED: <flow> — <risk>\`. If coverage is adequate reply \`COVERAGE: ADEQUATE\`."

ACTIVE_HOST="$(detect_orchestrator_host)"
ADVERSARY_KIND="${QA_COVERAGE_KIND:-$(adversary_kind_for_host "$ACTIVE_HOST")}"
FALLBACK_KIND="$(adversary_kind_for_host "$ADVERSARY_KIND")"

if [ "$ADVERSARY_KIND" = "codex" ]; then
  ADVERSARY_BIN_CODEX="${QA_COVERAGE_BIN:-${ADVERSARY_BIN_CODEX:-codex}}"
  MODEL="${QA_COVERAGE_MODEL:-gpt-5.6-terra}"
  EFFORT="${QA_COVERAGE_EFFORT:-high}"
else
  ADVERSARY_BIN_CLAUDE="${QA_COVERAGE_BIN:-${ADVERSARY_BIN_CLAUDE:-claude}}"
  MODEL="${QA_COVERAGE_CLAUDE_MODEL:-sonnet}"
  EFFORT=""
fi
if [ "$FALLBACK_KIND" = "codex" ]; then
  ADVERSARY_BIN_CODEX="${QA_COVERAGE_FALLBACK_BIN:-${ADVERSARY_BIN_CODEX:-codex}}"
  FALLBACK_MODEL="${QA_COVERAGE_MODEL:-gpt-5.6-terra}"
  FALLBACK_EFFORT="${QA_COVERAGE_EFFORT:-high}"
else
  ADVERSARY_BIN_CLAUDE="${QA_COVERAGE_FALLBACK_BIN:-${ADVERSARY_BIN_CLAUDE:-claude}}"
  FALLBACK_MODEL="${QA_COVERAGE_CLAUDE_MODEL:-sonnet}"
  FALLBACK_EFFORT=""
fi
export ADVERSARY_BIN_CODEX ADVERSARY_BIN_CLAUDE

OUTPUT="$(adversary_invoke_with_fallback "$ADVERSARY_KIND" "$FALLBACK_KIND" \
  "${QA_COVERAGE_TIMEOUT:-300}" "$MODEL" "$EFFORT" \
  "$FALLBACK_MODEL" "$FALLBACK_EFFORT" "$PROMPT" 2>&1)"
rc=$?
if [ $rc -ne 0 ]; then
  echo "[qa-coverage-adversary] both review hosts unavailable — skipped (advisory)"
  exit 0
fi

DEGRADED_LINE="$(printf '%s\n' "$OUTPUT" | grep -E '^adversary-host: DEGRADED ' | head -1)"
[ -z "$DEGRADED_LINE" ] || echo "[qa-coverage-adversary] $DEGRADED_LINE" >&2

MISSED="$(printf '%s\n' "$OUTPUT" | grep -E '^MISSED:' | head -20)"
ADEQUATE="$(printf '%s\n' "$OUTPUT" | grep -E '^COVERAGE: ADEQUATE[[:space:]]*$' | tail -1)"

if [ -n "$MISSED" ]; then
  echo "[qa-coverage-adversary] gaps found:"
  printf '%s\n' "$MISSED"
elif [ -n "$ADEQUATE" ]; then
  echo "[qa-coverage-adversary] $ADEQUATE"
else
  echo "[qa-coverage-adversary] no parseable coverage findings"
fi
exit 0
