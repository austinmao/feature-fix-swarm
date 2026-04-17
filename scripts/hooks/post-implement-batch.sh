#!/usr/bin/env bash
# PostToolUse: Debounced qa-only after batched edits in Ralph worktrees
# Trigger: Edit|Write on paths matching .claude/worktrees/phase-*
# Behavior: waits 30s after last edit, then fires qa-only in background
# Disable: export RALPH_AUTO_QA=0

set -euo pipefail

# Quick exit if disabled
[ "${RALPH_AUTO_QA:-1}" = "0" ] && exit 0

# Only trigger for edits in phase worktrees
TOOL_INPUT="${1:-}"
if ! echo "$TOOL_INPUT" | grep -q '.claude/worktrees/phase-'; then
  exit 0
fi

# Debounce: write a timestamp to a lock file
LOCK_DIR="/tmp/ralph-qa-debounce"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/last-edit"

# Record this edit timestamp
date +%s > "$LOCK_FILE"

# Check if a debounce watcher is already running
WATCHER_PID_FILE="$LOCK_DIR/watcher.pid"
if [ -f "$WATCHER_PID_FILE" ]; then
  OLD_PID=$(cat "$WATCHER_PID_FILE" 2>/dev/null || echo "0")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    # Watcher is alive — it will pick up the new timestamp
    exit 0
  fi
fi

# Spawn a background watcher that waits for 30s of quiet, then fires qa-only
(
  DEBOUNCE_SECS="${RALPH_DEBOUNCE_SECS:-30}"

  while true; do
    sleep "$DEBOUNCE_SECS"

    # Read last edit time
    if [ ! -f "$LOCK_FILE" ]; then
      break
    fi

    LAST_EDIT=$(cat "$LOCK_FILE" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    ELAPSED=$((NOW - LAST_EDIT))

    # If enough quiet time has passed, fire qa-only
    if [ "$ELAPSED" -ge "$DEBOUNCE_SECS" ]; then
      echo "[RALPH] Auto-QA: running qa-only on recent edits..." >&2

      # Find changed files in the worktree
      WORKTREE_PATH=$(echo "$TOOL_INPUT" | grep -oE '.claude/worktrees/phase-[^/]*' | head -1)
      if [ -n "$WORKTREE_PATH" ] && [ -d "$WORKTREE_PATH" ]; then
        CHANGED=$(cd "$WORKTREE_PATH" && git diff --name-only HEAD 2>/dev/null || echo "")
        if [ -n "$CHANGED" ]; then
          # Run qa-swarm on changed files only (qa-only scope)
          bash scripts/qa-swarm.sh \
            --phase "auto-qa" \
            --diff "$CHANGED" \
            --spec-dir "." \
            --qa-only "review" 2>&1 | while IFS= read -r line; do
              echo "[RALPH-AUTO] $line" >&2
            done
        fi
      fi

      # Clean up
      rm -f "$LOCK_FILE" "$WATCHER_PID_FILE" 2>/dev/null
      break
    fi
  done
) &

# Save watcher PID
echo $! > "$WATCHER_PID_FILE"

exit 0
