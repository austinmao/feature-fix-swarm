#!/usr/bin/env bash
# Trimmed-MCP headless gsd runner. Full user MCP config overflows 200k context
# (spike-proven) — run with an empty MCP server set instead.
# Usage: gsd-run.sh <slash-command> [args...]
set -uo pipefail

if [ $# -lt 1 ]; then
  echo "usage: gsd-run.sh <slash-command> [args...]" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || exit 1

TIMEOUT_SECS="${TIMEOUT:-900}"
LOG_DIR="$REPO_ROOT/.planning/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/gsd-run-${TS}.log"

CMD_STR="$*"

CLAUDE_ARGS=(--strict-mcp-config --mcp-config '{"mcpServers":{}}' --dangerously-skip-permissions -p "$CMD_STR")

if command -v timeout >/dev/null 2>&1; then
  timeout "$TIMEOUT_SECS" claude "${CLAUDE_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
  exit "${PIPESTATUS[0]}"
else
  claude "${CLAUDE_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
  exit "${PIPESTATUS[0]}"
fi
