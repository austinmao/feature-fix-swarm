#!/usr/bin/env bash
# Executor Detection — decides whether to use ruflo swarm or native Agent()
# Called by feature-implement to pick the right executor for QA swarm and task spawning
#
# Output: one word on stdout: "ruflo" or "native"
# Exit code: always 0

set -euo pipefail

# Check 1: Are we nested? (CLAUDE_AGENT_DEPTH > 0 means we're inside a sub-agent)
DEPTH="${CLAUDE_AGENT_DEPTH:-0}"
if [ "$DEPTH" -gt 0 ]; then
  # Nested context — Agent() won't work, must use ruflo
  echo "ruflo"
  exit 0
fi

# Check 2: Is ruflo available?
# Quick health check via MCP (the calling context handles this, but we provide advisory)
if command -v npx >/dev/null 2>&1; then
  # Check if ruflo MCP server is responsive (non-blocking, 5s timeout)
  # Note: This is advisory. The actual MCP call is made by the parent.
  # We just check if the binary is reachable.
  if npx claude-flow@v3alpha --version >/dev/null 2>&1; then
    echo "ruflo"
    exit 0
  fi
fi

# Check 3: User forced via env var
if [ "${RALPH_EXECUTOR:-}" = "ruflo" ]; then
  echo "ruflo"
  exit 0
elif [ "${RALPH_EXECUTOR:-}" = "native" ]; then
  echo "native"
  exit 0
fi

# Default: native Agent() at top level (proven, low risk)
echo "native"
exit 0
