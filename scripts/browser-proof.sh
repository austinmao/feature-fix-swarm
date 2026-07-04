#!/usr/bin/env bash
set -euo pipefail

# browser-proof.sh — resolve the browser-verification context for a phase
# (v3.20.0 Stream B). Emits directive lines the orchestrator parses:
#
#   WEB-TOUCH:yes|no          does this phase's diff touch web surfaces?
#   DRIVER:canary|playwright|agent   best available driver (trust-descending)
#   BASE-URL:<url>            reachable app URL to verify against
#   BROWSER-PROOF: READY|WAIVED|NOT-NEEDED|NO-SERVER
#
# Contract (pinned by browser-proof.bats):
#   - Non-web diff        -> NOT-NEEDED, exit 0 (browser QA genuinely n/a)
#   - Web diff, server up -> READY, exit 0
#   - Web diff, NO server -> NO-SERVER, exit 1  <- FAIL, never a silent skip.
#     The old qa-swarm behavior skipped e2e when :3000 didn't answer, letting
#     UI phases pass QA with zero browser verification.
#   - QA_ALLOW_NO_SERVER=1 -> WAIVED, exit 0 (explicit, visible waiver only)
#
# Env:
#   QA_BASE_URL                 pin the app URL (unreachable pin = NO-SERVER,
#                               it never falls through to probing)
#   BROWSER_PROOF_PROBE_PORTS   override probe list (default: common dev ports)
#   QA_ALLOW_NO_SERVER=1        waive the server requirement explicitly

DIFF_FILES=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --diff) DIFF_FILES="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done
[ -z "$DIFF_FILES" ] && { echo "ERROR: --diff required"; exit 2; }

# --- web-touch detection -----------------------------------------------
# UI extensions, page/route/component dirs, and API routes (auth/API breakage
# manifests in the browser). The decompose [qa:browser] tag overrides in both
# directions; this regex is the default when no tag is present.
WEB_RE='\.(tsx|jsx|vue|svelte|astro|html|css|scss|less)$|(^|/)(pages|routes|components|emails|templates|public)/|(^|/)app/.*(page|layout|route|template|error|not-found)\.|(^|/)api/'

WEB_TOUCH="no"
for f in $DIFF_FILES; do
  if echo "$f" | grep -qE "$WEB_RE"; then WEB_TOUCH="yes"; break; fi
done
echo "WEB-TOUCH:$WEB_TOUCH"

if [ "$WEB_TOUCH" = "no" ]; then
  echo "BROWSER-PROOF: NOT-NEEDED (no web surfaces in diff)"
  exit 0
fi

# --- base-url resolution -----------------------------------------------
probe() { # true iff a server answers (any HTTP status incl. 500 = server up)
  local code
  # --noproxy '*': a proxy answering for dead local ports would fake "server up".
  # curl -w prints "000" itself on connection failure — do NOT `|| echo 000`
  # here or the substitution captures both and "000\n000" reads as "up".
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 --noproxy '*' "$1" 2>/dev/null || true)
  [ -n "$code" ] && [ "$code" != "000" ]
}

BASE_URL=""
if [ -n "${QA_BASE_URL:-}" ]; then
  # A pinned URL is authoritative: if it's down, that IS the failure —
  # probing other ports would verify the wrong app.
  if probe "$QA_BASE_URL"; then BASE_URL="$QA_BASE_URL"; fi
else
  PORTS="${BROWSER_PROOF_PROBE_PORTS:-3000 3001 5173 4321 8080 8000}"
  for port in $PORTS; do
    if probe "http://127.0.0.1:$port"; then BASE_URL="http://127.0.0.1:$port"; break; fi
  done
fi

if [ -z "$BASE_URL" ]; then
  if [ "${QA_ALLOW_NO_SERVER:-0}" = "1" ]; then
    echo "BROWSER-PROOF: WAIVED (QA_ALLOW_NO_SERVER=1 — web diff left browser-unverified BY EXPLICIT WAIVER)"
    exit 0
  fi
  echo "BROWSER-PROOF: NO-SERVER — web-touching diff but no app answering."
  echo "  Start the app (or set QA_BASE_URL to a preview/prod URL), then re-run."
  echo "  Explicit waiver: QA_ALLOW_NO_SERVER=1 (recorded, never silent)."
  exit 1
fi
echo "BASE-URL:$BASE_URL"

# --- driver ladder (trust-descending) ------------------------------------
if command -v canary >/dev/null 2>&1; then
  DRIVER="canary"          # recorded session: trace/video/HAR/report.html
elif command -v playwright >/dev/null 2>&1 \
  || [ -d node_modules/@playwright/test ] || [ -d node_modules/playwright ] \
  || [ -d web/node_modules/@playwright/test ]; then
  DRIVER="playwright"      # scripted run, JSON reporter
else
  DRIVER="agent"           # LLM-driven browser; weakest tier, strict-rejectable
fi
echo "DRIVER:$DRIVER"
echo "BROWSER-PROOF: READY"
exit 0
