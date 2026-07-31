#!/usr/bin/env bash
# gsd-checkpoint.sh — PostToolUse HANDOFF writer (borrowed: buildomator).
#
# Claude Code's *microcompact* strips stale tool outputs WITHOUT firing
# PreCompact, and read-heavy phases write no files — so PreCompact-only
# checkpointing leaves resume state arbitrarily stale after a usage-cap kill,
# network drop, or silent compaction. This hook refreshes
# .planning/run-state/HANDOFF.json after tool calls, throttled to at most once
# per 60s via the file's own mtime, keeping resume state <=60s stale.
#
# Captured: ts (ISO-8601 UTC), branch, head sha, dirty-file count, and the
# STATE.md position line — enough for /gsd-resume-work (or an /adopt-wip
# triage) to re-anchor without trusting a dead session's memory.
#
# Contract: ALWAYS exit 0 (a broken checkpoint must never fail a tool call).
# Inert until a consumer wires it (PostToolUse matcher) in settings.json.
# Kill-switch: GSD_CHECKPOINT=off.
set -uo pipefail

[ "${GSD_CHECKPOINT:-on}" = "off" ] && exit 0
[ -d .planning ] || exit 0

HANDOFF=".planning/run-state/HANDOFF.json"

# Throttle on the handoff's own mtime (macOS stat -f / GNU stat -c both tried).
if [ -f "$HANDOFF" ]; then
  mtime="$(stat -f %m "$HANDOFF" 2>/dev/null || stat -c %Y "$HANDOFF" 2>/dev/null || echo 0)"
  now="$(date +%s)"
  [ $((now - mtime)) -lt 60 ] && exit 0
fi

mkdir -p .planning/run-state 2>/dev/null || exit 0

branch="$(git branch --show-current 2>/dev/null || echo unknown)"
head="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
dirty="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
state_head="$(grep -m1 '^## ' .planning/STATE.md 2>/dev/null | sed 's/"/\\"/g' || true)"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Temp + rename so a mid-write kill never leaves a truncated handoff — the one
# file whose job is surviving exactly those kills.
tmp="$HANDOFF.tmp"
printf '{"ts":"%s","branch":"%s","head":"%s","dirty":%s,"state_head":"%s","resume":"run /gsd-resume-work"}\n' \
  "$ts" "$branch" "$head" "${dirty:-0}" "$state_head" > "$tmp" 2>/dev/null \
  && mv "$tmp" "$HANDOFF" 2>/dev/null

exit 0
