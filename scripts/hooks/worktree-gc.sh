#!/usr/bin/env bash
# Worktree Garbage Collection — runs on SessionStart
# Removes .claude/worktrees/phase-* dirs older than 24h with no active tmux session
# Safe: only touches phase worktrees created by the Ralph loop, never main/feature branches

set -euo pipefail

WORKTREE_BASE=".claude/worktrees"

# Exit silently if no worktree base exists
[ -d "$WORKTREE_BASE" ] || exit 0

CLEANED=0
KEPT=0

for wt_dir in "$WORKTREE_BASE"/phase-*; do
  [ -d "$wt_dir" ] || continue

  # Check age: older than 24h?
  if [ "$(find "$wt_dir" -maxdepth 0 -mmin +1440 2>/dev/null)" ]; then
    WS_NAME=$(basename "$wt_dir")

    # Check for active tmux session with this worktree name
    if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$WS_NAME" 2>/dev/null; then
      KEPT=$((KEPT + 1))
      continue
    fi

    # Safe to remove — git worktree remove if registered, rm -rf otherwise
    if git worktree list --porcelain 2>/dev/null | grep -q "$wt_dir"; then
      git worktree remove --force "$wt_dir" 2>/dev/null || rm -rf "$wt_dir"
    else
      rm -rf "$wt_dir"
    fi
    CLEANED=$((CLEANED + 1))
  else
    KEPT=$((KEPT + 1))
  fi
done

# Report only if something happened
[ "$CLEANED" -gt 0 ] && echo "[RALPH] Cleaned $CLEANED stale worktrees ($KEPT kept)" || true
