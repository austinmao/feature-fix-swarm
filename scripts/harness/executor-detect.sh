#!/usr/bin/env bash
# Executor Detection — decides whether to use Ruflo coordination or direct host CLI
# Called by feature-implement to pick the coordination layer for QA and task spawning
#
# Output: one word on stdout: "ruflo" or "host-cli"
# Exit code: always 0

set -euo pipefail

# Check 1: Are we nested? (CLAUDE_AGENT_DEPTH > 0 means we're inside a sub-agent)
DEPTH="${CLAUDE_AGENT_DEPTH:-0}"
if [ "$DEPTH" -gt 0 ]; then
  # Nested context — keep coordination out-of-band through Ruflo.
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
elif [ "${RALPH_EXECUTOR:-}" = "host-cli" ] || [ "${RALPH_EXECUTOR:-}" = "native" ]; then
  echo "host-cli"
  exit 0
fi

# Default: direct host CLI at top level; Ruflo remains opt-in/defaulted by skill policy.
echo "host-cli"
exit 0
