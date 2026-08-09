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
#
# Documented limitation: evidence is bound by FRESHNESS only — this gate does
# NOT verify the results came from this revision/base-URL/scenario-set. That
# binding lives in `python3 lib/runtime_proof.py verify` when a spec declares
# proof scenarios; full provenance binding is a recorded follow-up.
set -euo pipefail

# REQ-301 (locked row 9 / C6): the commit identity bound into canary evidence
# comes SOLELY from git in this trusted wrapper — never from results.json.
# Captured at gate ENTRY and re-verified after the completeness check: any
# drift means the results were judged against a different tree than the one
# being recorded, so the gate refuses rather than binding the wrong sha.
CANARY_PRE_SHA="$(git rev-parse HEAD 2>/dev/null || true)"

# Default diff-base resolves through base-branch.sh — a hardcoded origin/main
# diffs against a nonexistent ref on master/trunk repos.
# Fail-soft (`|| echo main`): on a crippled PATH the gate must still reach its
# own fail-closed checks instead of dying 127 in this default init.
DIFF_BASE="${CANARY_DIFF_BASE:-origin/$(bash "$(dirname "${BASH_SOURCE[0]:-$0}")/base-branch.sh" 2>/dev/null || echo main)}"
RESULTS_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --diff-base)
      if [ $# -lt 2 ]; then
        echo "canary-gate: usage: canary-gate.sh [--diff-base <ref>] [<results.json>]" >&2
        exit 2
      fi
      DIFF_BASE="$2"
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
  if ! "$(dirname "${BASH_SOURCE[0]:-$0}")/waiver-record.sh" \
      "canary-gate" "CANARY_GATE=off"; then
    echo "canary-gate: WAIVER-UNRECORDED — refusing disabled bypass" >&2
    exit 1
  fi
  echo "canary-gate: disabled (CANARY_GATE=off)"
  exit 0
fi

WEB_PATTERN="${CANARY_WEB_PATTERN:-\.(tsx|jsx|vue|svelte|astro|html|css|scss|less)$|(^|/)(pages|routes|components|emails|templates|public|hooks|stores?|styles?)/|(^|/)app/|(^|/)api/|(^|/)(tailwind|next|nuxt|vite|astro|svelte)\.config\.}"
# Shell/bats files are never a browser surface even inside web-named dirs
# (scripts/hooks/*.sh matched the bare hooks/ alternation — false web-touch).
# Keep in lockstep with browser-proof.sh's WEB_EXCLUDE_RE.
WEB_EXCLUDE="${CANARY_WEB_EXCLUDE:-\.(sh|bash|bats)$}"

if ! git rev-parse --verify "${DIFF_BASE}^{commit}" >/dev/null 2>&1; then
  echo "canary-gate: FAIL — diff base '${DIFF_BASE}' unresolvable (fetch it or pass --diff-base)" >&2
  exit 1
fi

# quotePath=false: non-ASCII filenames arrive raw instead of C-quoted
# ("\303\251.tsx"), which would evade the extension anchor. Limitation:
# newline-in-filename remains pathological/unhandled by the line loop.
DIFF_FILES="$(git -c core.quotePath=false diff --name-only "${DIFF_BASE}...HEAD" 2>/dev/null || true)"

WEB_TOUCH="no"
if [ -n "$DIFF_FILES" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if echo "$f" | grep -Eq "$WEB_PATTERN" && ! echo "$f" | grep -Eq "$WEB_EXCLUDE"; then
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
# Platform-select stat: GNU takes -c %Y; BSD/macOS takes -f %m. GNU's -f is
# "filesystem status" and treats %m as an operand — partial garbage output —
# so try -c first, capture atomically, then validate numeric (fail-closed).
HEAD_TIME="$(git log -1 --format=%ct 2>/dev/null || echo 0)"
if ! RESULTS_MTIME="$(stat -c %Y "$RESULTS" 2>/dev/null)"; then
  RESULTS_MTIME="$(stat -f %m "$RESULTS" 2>/dev/null || echo 0)"
fi
if ! [[ "$RESULTS_MTIME" =~ ^[0-9]+$ ]]; then
  echo "canary-gate: FAIL — could not determine results mtime (stat output nonnumeric)" >&2
  exit 1
fi
if [ "${CANARY_GATE_ALLOW_STALE:-0}" != "1" ] && [ "$RESULTS_MTIME" -lt "$HEAD_TIME" ]; then
  echo "canary-gate: FAIL — stale canary results (older than HEAD)" >&2
  exit 1
fi
if [ "${CANARY_GATE_ALLOW_STALE:-0}" = "1" ] && [ "$RESULTS_MTIME" -lt "$HEAD_TIME" ]; then
  if ! "$(dirname "${BASH_SOURCE[0]:-$0}")/waiver-record.sh" \
      "canary-gate" "CANARY_GATE_ALLOW_STALE=1"; then
    echo "canary-gate: WAIVER-UNRECORDED — refusing stale-results bypass" >&2
    exit 1
  fi
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

# Completeness (fail-closed): a bare {"status":"passed"} must not pass — all
# summary counts must be present, numeric, with a nonzero total fully passed.
if ! jq -e '
  (.summary.stepsTotal | type == "number") and
  (.summary.stepsPassed | type == "number") and
  (.summary.stepsFailed | type == "number") and
  (.summary.consoleErrors | type == "number") and
  (.summary.networkFailures | type == "number") and
  (.summary.stepsTotal > 0) and
  (.summary.stepsPassed == .summary.stepsTotal) and
  (.summary.stepsFailed == 0) and
  (.summary.consoleErrors == 0) and
  (.summary.networkFailures == 0)
' "$RESULTS" >/dev/null 2>&1; then
  echo "canary-gate: FAIL — incomplete or failing summary (need numeric stepsTotal>0, stepsPassed==stepsTotal, stepsFailed==0, consoleErrors==0, networkFailures==0)" >&2
  exit 1
fi

# ── spec-008 Phase 3 (REQ-301): typed canary evidence, fail-closed ──────────
# A durable {run_id, sha, pass, created_at, ended_at, ts} row must land in
# the evidence store BEFORE PASS is printed. Any failure to record refuses
# the pass (same posture as REQ-201's WAIVER-UNRECORDED).

CANARY_POST_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
if [ -z "$CANARY_PRE_SHA" ] || [ "$CANARY_PRE_SHA" != "$CANARY_POST_SHA" ]; then
  echo "canary-gate: CANARY-IDENTITY-DRIFT — HEAD changed between gate entry (${CANARY_PRE_SHA:-unknown}) and evidence recording (${CANARY_POST_SHA:-unknown}); refusing to record" >&2
  exit 1
fi

# Timestamps come from the already-validated results.json (createdAt/endedAt).
# Timestamps are not identity (C6) — the sha above never comes from this file.
CANARY_CREATED_AT="$(jq -r '.createdAt // empty' "$RESULTS" 2>/dev/null || true)"
CANARY_ENDED_AT="$(jq -r '.endedAt // empty' "$RESULTS" 2>/dev/null || true)"
if [ -z "$CANARY_CREATED_AT" ] || [ -z "$CANARY_ENDED_AT" ]; then
  echo "canary-gate: CANARY-EVIDENCE-UNRECORDED — results.json lacks createdAt/endedAt timestamps; refusing to PASS without a durable record" >&2
  exit 1
fi

# Recorder invocation mirrors the waiver-record.sh adapter shape (python3 +
# lib/gates.py resolved from this script's OWN repo root, never the caller's
# cwd) but PRESERVES GATES_STORE so tests and runs land in the active store.
CANARY_GATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
CANARY_GATE_ROOT="$(git -C "$CANARY_GATE_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$CANARY_GATE_ROOT" ] || [ ! -f "$CANARY_GATE_ROOT/lib/gates.py" ]; then
  echo "canary-gate: CANARY-EVIDENCE-UNRECORDED — cannot resolve lib/gates.py from the gate's own repo; refusing to PASS" >&2
  exit 1
fi
if ! (cd "$CANARY_GATE_ROOT" && env -u GIT_DIR -u GIT_COMMON_DIR -u GIT_WORK_TREE \
    python3 "$CANARY_GATE_ROOT/lib/gates.py" canary-evidence \
    --run-id "${GSD_RUN_ID:-unattributed}" --sha "$CANARY_PRE_SHA" \
    --created-at "$CANARY_CREATED_AT" --ended-at "$CANARY_ENDED_AT" \
    --pass true); then
  echo "canary-gate: CANARY-EVIDENCE-UNRECORDED — recorder failed; refusing to PASS without a durable record" >&2
  exit 1
fi

echo "canary-gate: PASS (steps ${STEPS_PASSED}/${STEPS_TOTAL}, consoleErrors 0, networkFailures 0)"
exit 0
