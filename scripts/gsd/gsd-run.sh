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

# gsd's mempalace commands call a bare `mempalace` binary in headless mode —
# resolve to the repo's gbrain-backed shim (spec 002 Phase D).
export PATH="$REPO_ROOT/scripts/gsd:$PATH"

TIMEOUT_SECS="${TIMEOUT:-900}"
LOG_DIR="$REPO_ROOT/.planning/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/gsd-run-${TS}.log"

CMD_STR="$*"

# Lead model: the headless lead is mechanical orchestration (reads the gsd
# workflow, spawns subagents) — sonnet-class. Running it on the session default
# (often opus) cost $17.73 of a $51.54 spec-256 conformance run for zero
# quality gain; subagent tiers come from .planning/config.json, not the lead.
LEAD_MODEL="${GSD_LEAD_MODEL:-claude-sonnet-5}"

CLAUDE_ARGS=(--strict-mcp-config --mcp-config '{"mcpServers":{}}' --dangerously-skip-permissions --model "$LEAD_MODEL" -p "$CMD_STR")

# A shell-exported ANTHROPIC_API_KEY overrides the claude.ai OAuth login and
# 401s headless drives — scrub auth env so the CLI uses its own login.
RUN=(env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN claude)

if command -v timeout >/dev/null 2>&1; then
  timeout "$TIMEOUT_SECS" "${RUN[@]}" "${CLAUDE_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
  exit "${PIPESTATUS[0]}"
else
  "${RUN[@]}" "${CLAUDE_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
  exit "${PIPESTATUS[0]}"
fi
