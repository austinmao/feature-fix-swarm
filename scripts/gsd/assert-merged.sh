#!/usr/bin/env bash
# assert-merged.sh — terminal-state assertion after a PR merge.
#
# A `gh pr merge` that races a branch-protection failure, or an operator
# closing instead of merging, leaves the PR CLOSED — work NOT landed — while
# the orchestrator happily records merge:pr consumed. This lever makes the
# terminal state machine-checked: run it after every merge, before recording
# the merge as done. (Pattern: steipete/agent-scripts landpr "verify
# state==MERGED, never CLOSED".)
#
# Usage: assert-merged.sh <pr-number> [<repo owner/name>]
# Exit: 0 = MERGED · 1 = CLOSED without merge (loud) · 2 = still OPEN · 3 = gh error
set -euo pipefail

PR="${1:?usage: assert-merged.sh <pr-number> [<owner/repo>]}"
REPO_ARGS=()
[ -n "${2:-}" ] && REPO_ARGS=(--repo "$2")

STATE="$(gh pr view "$PR" ${REPO_ARGS[@]+"${REPO_ARGS[@]}"} --json state,mergedAt \
  --jq '.state + " " + (.mergedAt // "null")' 2>/dev/null)" || {
  echo "[assert-merged] ERROR: gh pr view $PR failed (auth? repo? number?)" >&2
  exit 3
}

case "$STATE" in
  "MERGED "*)
    echo "[assert-merged] PR #$PR MERGED (mergedAt ${STATE#MERGED })"
    exit 0 ;;
  "CLOSED null")
    echo "[assert-merged] FAIL: PR #$PR is CLOSED WITHOUT merge — work NOT landed. Do not record merge:pr as consumed." >&2
    exit 1 ;;
  "OPEN "*)
    echo "[assert-merged] PR #$PR still OPEN — merge has not completed" >&2
    exit 2 ;;
  *)
    echo "[assert-merged] FAIL: unexpected state '$STATE' for PR #$PR" >&2
    exit 1 ;;
esac
