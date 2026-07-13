#!/usr/bin/env bash
# gsd workflow.code_review_command target — invoked by /gsd:ship.
# Contract (installed docs): node_modules/@opengsd/gsd-core/gsd-core/references/planning-config.md:272 —
#   "The diff is piped to the command via stdin; the command must output JSON with a
#    `verdict` field (`"APPROVED"` or `"REVISE"`). Non-zero exit or `"REVISE"` verdict
#    blocks the ship workflow."
# NOTE: this is stdin-diff + stdout-JSON, NOT file-paths-on-stdin — verified above,
# overriding the file-paths assumption in the original task brief.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/adversary-host.sh"

DIFF="$(cat)"

# Autonomy-grant wall (fail-closed): ship is an operator-gated action. This seam
# runs inside /gsd:ship and a REVISE verdict blocks it, so the grant ledger check
# lives here (day-1 wrapper; native ship:pre capability gate deferred — no
# third-party gate evaluator at gsd-core 1.6.1, upstream #2004).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
# RUN_ID: env (exported by feature-implement) > branch-derived spec-NNN.
# No hardcoded default — a wrong ledger key silently checks another run's
# grants. Underivable → fail-closed REVISE.
RUN_ID="${GSD_RUN_ID:-}"
if [ -z "$RUN_ID" ]; then
  BRANCH_NNN="$(git branch --show-current 2>/dev/null | grep -oE '^[0-9]{3}' | head -1)"
  [ -n "$BRANCH_NNN" ] && RUN_ID="spec-${BRANCH_NNN}"
fi
if [ -z "$RUN_ID" ]; then
  echo '{"verdict":"REVISE","note":"BLOCKED: GSD_RUN_ID unset and branch has no NNN prefix — export GSD_RUN_ID=spec-NNN so the grant ledger keys correctly"}'
  exit 1
fi
GATES_PY=""
for candidate in \
  "$REPO_ROOT/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$REPO_ROOT/lib/gates.py"; do
  [ -f "$candidate" ] && GATES_PY="$candidate" && break
done
if [ -n "$GATES_PY" ]; then
  # Typed action ('type:target') — gates.py rejects free prose (gates.py:588-592)
  if ! python3 "$GATES_PY" check-grant "$RUN_ID" --action "ship:gsd" >/dev/null 2>&1; then
    python3 "$GATES_PY" pending "$RUN_ID" --action "ship:gsd" \
      --reason "gsd ship attempted without a ship grant" >/dev/null 2>&1 || true
    echo '{"verdict":"REVISE","note":"BLOCKED: no ship grant in autonomy-grant ledger for '"$RUN_ID"' (pending recorded) — run /autonomy-grant"}'
    exit 1
  fi
fi

if [ -z "$DIFF" ]; then
  echo '{"verdict":"APPROVED","note":"empty diff"}'
  exit 0
fi

PROMPT="Review this diff. Report ONLY CRITICAL or HIGH severity findings (security, correctness, data loss). End your response with exactly one line: VERDICT: PASS or VERDICT: BLOCK.

--- DIFF START ---
${DIFF}
--- DIFF END ---"

ACTIVE_HOST="$(detect_orchestrator_host)" || {
  echo '{"verdict":"REVISE","note":"BLOCKED: review host detection failed"}'
  exit 1
}
REVIEW_KIND="$(adversary_kind_for_host "$ACTIVE_HOST")"

review_model_for_kind() {
  if [ "$1" = "codex" ]; then
    REVIEW_MODEL="gpt-5.6-sol"
    REVIEW_EFFORT="xhigh"
  else
    REVIEW_MODEL="opus"
    REVIEW_EFFORT=""
  fi
}

# Reviews are read-only, so one fallback to the active host is safe. The shared
# helper gives both attempts one overall deadline and never feeds the failed
# transcript to the fallback reviewer.
review_model_for_kind "$REVIEW_KIND"
PREFERRED_MODEL="$REVIEW_MODEL"
PREFERRED_EFFORT="$REVIEW_EFFORT"
FALLBACK_KIND="$ACTIVE_HOST"
review_model_for_kind "$FALLBACK_KIND"
FALLBACK_MODEL="$REVIEW_MODEL"
FALLBACK_EFFORT="$REVIEW_EFFORT"
OUTPUT="$(adversary_invoke_with_fallback "$REVIEW_KIND" "$FALLBACK_KIND" \
  "${GSD_REVIEW_TIMEOUT:-600}" "$PREFERRED_MODEL" "$PREFERRED_EFFORT" \
  "$FALLBACK_MODEL" "$FALLBACK_EFFORT" "$PROMPT" 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo '{"verdict":"REVISE","note":"BLOCKED: both review hosts unavailable; no review verdict"}'
  exit 1
fi
DEGRADED=0
printf '%s\n' "$OUTPUT" | grep -q '^adversary-host: DEGRADED ' && DEGRADED=1

# Exactly one line-anchored verdict is accepted. It need not be the physical
# final line because vendor CLIs may append usage diagnostics after assistant
# output. This still rejects prompt echo, duplicates, and prose substrings.
CLEAN_OUTPUT="$(printf '%s\n' "$OUTPUT" | tr -d '\r')"
VERDICT_COUNT="$(printf '%s\n' "$CLEAN_OUTPUT" | grep -Ec '^VERDICT: (PASS|BLOCK)[[:space:]]*$' || true)"
FINAL_VERDICT="$(printf '%s\n' "$CLEAN_OUTPUT" | grep -E '^VERDICT: (PASS|BLOCK)[[:space:]]*$' || true)"
if [ "$VERDICT_COUNT" -ne 1 ]; then
  echo '{"verdict":"REVISE","note":"BLOCKED: reviewer returned rc=0 but missing final anchored verdict (or returned multiple verdicts)"}'
  exit 1
fi

if printf '%s\n' "$FINAL_VERDICT" | grep -Eq '^VERDICT: BLOCK[[:space:]]*$'; then
  printf '{"verdict":"REVISE","findings":%s}\n' "$(printf '%s' "$CLEAN_OUTPUT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  exit 1
fi

if [ "$DEGRADED" -eq 1 ]; then
  echo '{"verdict":"APPROVED","note":"DEGRADED: opposite-host unavailable; active-host fallback returned one anchored PASS"}'
else
  echo '{"verdict":"APPROVED"}'
fi
exit 0
