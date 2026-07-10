#!/usr/bin/env bash
# canary-gate.sh — feature-fix-swarm fail-closed browser-QA gate.
#
# Web-touching diffs MUST carry a fresh, passing Canary (@usecanary/cli)
# session before a phase counts as done. No results = FAIL, never a silent
# skip — same fail-closed discipline as scripts/browser-proof.sh's
# NO-SERVER case, but at the recorded-results-artifact layer.
#
# Canary CLI writes ~/.canary/sessions/<id>/results.json shaped:
#   {"status":"passed"|"failed","summary":{"stepsTotal":N,"stepsPassed":N,
#    "stepsFailed":N,"consoleErrors":N,"networkFailures":N},"steps":[...]}
#
# Usage: canary-gate.sh [--diff-base <ref>] [<results.json>]
#   <results.json> falls back to $CANARY_RESULTS when no positional arg.
#
# Env:
#   CANARY_GATE=off             kill-switch — exit 0 unconditionally
#   CANARY_DIFF_BASE            default diff-base ref (default: origin/main)
#   CANARY_WEB_PATTERN          override the WEB-TOUCH extended-regex
#   CANARY_RESULTS              results.json path when no positional arg
#   CANARY_GATE_ALLOW_STALE=1   bypass the staleness check only
#
# WEB-TOUCH pattern reused VERBATIM from scripts/browser-proof.sh's WEB_RE
# (v3.20.0 Stream B) so both gates agree on what counts as a web surface.
set -euo pipefail

DIFF_BASE="${CANARY_DIFF_BASE:-origin/main}"
RESULTS_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --diff-base)
      DIFF_BASE="${2:-}"
      shift 2
      ;;
    -*)
      echo "canary-gate: usage: canary-gate.sh [--diff-base <ref>] [<results.json>]" >&2
      exit 2
      ;;
    *)
      RESULTS_ARG="$1"
      shift
      ;;
  esac
done

if [ "${CANARY_GATE:-on}" = "off" ]; then
  echo "canary-gate: disabled (CANARY_GATE=off)"
  exit 0
fi

WEB_PATTERN="${CANARY_WEB_PATTERN:-\.(tsx|jsx|vue|svelte|astro|html|css|scss|less)$|(^|/)(pages|routes|components|emails|templates|public|hooks|stores?|styles?)/|(^|/)app/|(^|/)api/|(^|/)(tailwind|next|nuxt|vite|astro|svelte)\.config\.}"

DIFF_FILES="$(git diff --name-only "${DIFF_BASE}...HEAD" 2>/dev/null || true)"

WEB_TOUCH="no"
if [ -n "$DIFF_FILES" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if echo "$f" | grep -Eq "$WEB_PATTERN"; then
      WEB_TOUCH="yes"
      break
    fi
  done <<< "$DIFF_FILES"
fi

if [ "$WEB_TOUCH" = "no" ]; then
  echo "canary-gate: NOT-NEEDED (no web-touch in diff)"
  exit 0
fi

RESULTS="${RESULTS_ARG:-${CANARY_RESULTS:-}}"
if [ -z "$RESULTS" ] || [ ! -f "$RESULTS" ]; then
  echo "canary-gate: FAIL — web-touch diff but no canary results (run a canary session or set CANARY_RESULTS)" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "canary-gate: FAIL — jq required to parse canary results" >&2
  exit 1
fi

# Staleness: results.json must be at least as new as HEAD's commit.
HEAD_TIME="$(git log -1 --format=%ct 2>/dev/null || echo 0)"
RESULTS_MTIME="$(stat -f %m "$RESULTS" 2>/dev/null || stat -c %Y "$RESULTS" 2>/dev/null || echo 0)"
if [ "${CANARY_GATE_ALLOW_STALE:-0}" != "1" ] && [ "$RESULTS_MTIME" -lt "$HEAD_TIME" ]; then
  echo "canary-gate: FAIL — stale canary results (older than HEAD)" >&2
  exit 1
fi

STATUS="$(jq -r '.status // empty' "$RESULTS" 2>/dev/null || true)"
STEPS_TOTAL="$(jq -r '.summary.stepsTotal // 0' "$RESULTS" 2>/dev/null || echo 0)"
STEPS_PASSED="$(jq -r '.summary.stepsPassed // 0' "$RESULTS" 2>/dev/null || echo 0)"
CONSOLE_ERRORS="$(jq -r '.summary.consoleErrors // 0' "$RESULTS" 2>/dev/null || echo 0)"
NETWORK_FAILURES="$(jq -r '.summary.networkFailures // 0' "$RESULTS" 2>/dev/null || echo 0)"

if [ "$STATUS" != "passed" ] || [ "$CONSOLE_ERRORS" != "0" ] || [ "$NETWORK_FAILURES" != "0" ]; then
  echo "canary-gate: FAIL — status=${STATUS:-unknown} consoleErrors=${CONSOLE_ERRORS} networkFailures=${NETWORK_FAILURES}" >&2
  exit 1
fi

echo "canary-gate: PASS (steps ${STEPS_PASSED}/${STEPS_TOTAL}, consoleErrors 0, networkFailures 0)"
exit 0
