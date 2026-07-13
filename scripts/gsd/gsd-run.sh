#!/usr/bin/env bash
# Host-native headless GSD runner. Claude uses a trimmed MCP config because a
# full user MCP config overflows 200k context (spike-proven); Codex invokes the
# GSD skill installed for Codex and stays on Codex models.
# Usage: gsd-run.sh <slash-command> [args...]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/adversary-host.sh"
. "$SCRIPT_DIR/model-equivalents.sh"

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

ACTIVE_HOST="$(detect_orchestrator_host)" || exit $?
LEAD_TIER="${GSD_LEAD_MODEL:-sonnet}"

if [ "$ACTIVE_HOST" = "codex" ]; then
  CODEX_BIN="${CODEX_BIN:-codex}"
  if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
    echo "gsd-run: Codex CLI not found ($CODEX_BIN); refusing to cross to Claude" >&2
    exit 127
  fi

  if ! LEAD_MODEL="$(codex_equiv_model "$LEAD_TIER")"; then
    LEAD_MODEL="$LEAD_TIER"
  fi
  if ! LEAD_EFFORT="$(codex_equiv_effort "$LEAD_TIER")"; then
    LEAD_EFFORT="${GSD_LEAD_EFFORT:-high}"
  fi
  LEAD_EFFORT="${GSD_LEAD_EFFORT:-$LEAD_EFFORT}"

  # GSD's Codex installer emits per-agent TOMLs. Translate FFS's Claude-tier
  # aliases immediately before a drive so explicit planner/executor pins never
  # leak into Codex as invalid model IDs.
  CODEX_CONFIG_ROOT="${GSD_CODEX_CONFIG_ROOT:-}"
  if [ -z "$CODEX_CONFIG_ROOT" ]; then
    if [ -d "$REPO_ROOT/.codex/agents" ]; then
      CODEX_CONFIG_ROOT="$REPO_ROOT/.codex"
    else
      CODEX_CONFIG_ROOT="${CODEX_HOME:-$HOME/.codex}"
    fi
  fi
  if [ -x "$SCRIPT_DIR/codex-model-sync.sh" ]; then
    "$SCRIPT_DIR/codex-model-sync.sh" "$CODEX_CONFIG_ROOT" || {
      echo "gsd-run: Codex model materialization failed" >&2
      exit 1
    }
  fi

  first="$1"
  shift
  case "$first" in
    /gsd-*) CODEX_COMMAND="\$${first#/}" ;;
    \$gsd-*) CODEX_COMMAND="$first" ;;
    *)
      echo "gsd-run: unsupported Codex GSD command: $first" >&2
      exit 2
      ;;
  esac
  [ "$#" -eq 0 ] || CODEX_COMMAND="$CODEX_COMMAND $*"

  RUN=("$CODEX_BIN" exec
    -c "model=\"$LEAD_MODEL\""
    -c "model_reasoning_effort=\"$LEAD_EFFORT\""
    --dangerously-bypass-approvals-and-sandbox
    "$CODEX_COMMAND")
else
  CLAUDE_BIN="${CLAUDE_BIN:-claude}"
  if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    echo "gsd-run: Claude CLI not found ($CLAUDE_BIN); refusing to cross to Codex" >&2
    exit 127
  fi
  if ! LEAD_MODEL="$(claude_equiv_model "$LEAD_TIER")"; then
    case "$LEAD_TIER" in
      sonnet|claude-sonnet-*) LEAD_MODEL="claude-sonnet-5" ;;
      opus|claude-opus-*) LEAD_MODEL="claude-opus-4-8" ;;
      fable|claude-fable-*) LEAD_MODEL="claude-fable-5" ;;
      haiku|claude-haiku-*) LEAD_MODEL="claude-haiku-4-5-20251001" ;;
      *) LEAD_MODEL="$LEAD_TIER" ;;
    esac
  fi
  CMD_STR="$*"
  CLAUDE_ARGS=(--strict-mcp-config --mcp-config '{"mcpServers":{}}' --dangerously-skip-permissions --model "$LEAD_MODEL" -p "$CMD_STR")
  # A shell-exported Anthropic key overrides the claude.ai OAuth login and can
  # 401 headless drives — scrub auth env so the CLI uses its own login.
  RUN=(env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}")
fi

run_bounded "$TIMEOUT_SECS" "${RUN[@]}" </dev/null 2>&1 | tee "$LOG_FILE"
exit "${PIPESTATUS[0]}"
