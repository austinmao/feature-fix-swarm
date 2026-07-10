#!/usr/bin/env bash
# adversary-host.sh — sourceable lib: detect which vendor CLI is the
# orchestrating harness (claude vs codex) and invoke the OPPOSITE vendor
# as the cross-model adversary. Convention: skills/review-gate/SKILL.md
# "Pre-check — choose the opposite CLI" (~L57-90).
#
# Sourced by plan-adversary.sh and qa-coverage-adversary.sh. No side effects
# on source (function definitions only) — safe to source under `set -u`.

detect_orchestrator_host() {
  if [ -n "${CODEX_SESSION_ID:-}" ] || [ -n "${CODEX_HOME:-}" ] || [ -n "${CODEX_AGENT:-}" ]; then
    echo "codex"
  elif [ -n "${CLAUDE_SESSION_ID:-}" ] || [ -n "${CLAUDE_CODE:-}" ]; then
    echo "claude"
  else
    echo "claude"
  fi
}

adversary_kind_for_host() {
  case "${1:-}" in
    claude) echo "codex" ;;
    codex) echo "claude" ;;
    *) echo "codex" ;;
  esac
}

# adversary_invoke <kind> <timeout> <model> <effort> <prompt>
# Prints the underlying command's stdout+stderr and returns its exit code.
# effort is unused for kind=claude — accepted and ignored. rc=127 if the
# resolved bin is absent (fail-soft signal to the caller).
adversary_invoke() {
  local kind="$1" timeout_s="$2" model="$3" effort="$4" prompt="$5"
  if [ "$kind" = "codex" ]; then
    local bin="${ADVERSARY_BIN_CODEX:-codex}"
    if ! command -v "$bin" >/dev/null 2>&1; then
      return 127
    fi
    timeout "$timeout_s" "$bin" exec \
      -c "model=\"$model\"" -c "model_reasoning_effort=\"$effort\"" \
      "$prompt" </dev/null
    return $?
  else
    local bin="${ADVERSARY_BIN_CLAUDE:-claude}"
    if ! command -v "$bin" >/dev/null 2>&1; then
      return 127
    fi
    env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
      timeout "$timeout_s" "$bin" -p "$prompt" --model "$model" </dev/null
    return $?
  fi
}
