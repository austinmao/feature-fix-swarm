#!/usr/bin/env bash
# adversary-host.sh — sourceable lib: detect which vendor CLI is the
# orchestrating harness (claude vs codex) and invoke the OPPOSITE vendor
# as the cross-model adversary. Convention: skills/review-gate/SKILL.md
# "Pre-check — choose the opposite CLI" (~L57-90).
#
# Sourced by plan-adversary.sh and qa-coverage-adversary.sh. No side effects
# on source (function definitions only) — safe to source under `set -u`.
#
# Injection surface: adversary calls are data-only reviews, but the prompt
# content is UNTRUSTED (plan text, QA step names — attacker-influenceable).
# The codex sandbox is therefore pinned read-only (sandbox_mode below) so a
# hostile prompt can't drive writes/exec; claude -p runs non-interactive,
# where tool permission prompts auto-deny.

# Wall-clock bounding: run_bounded (timeout -> gtimeout -> python3 -> refuse
# rc-124). NEVER run the CLI unwrapped — an unbounded hang is a silent forever
# block (2026-07-12 dead-codex forensics); a refused/timed-out call returns
# 124 and the callers' existing fail-open paths fire.
. "$(dirname "${BASH_SOURCE[0]}")/run-bounded.sh"

detect_orchestrator_host() {
  case "${FFS_HOST:-}" in
    codex|claude)
      echo "$FFS_HOST"
      return 0
      ;;
    "") ;;
    *)
      echo "adversary-host: ignoring invalid FFS_HOST=$FFS_HOST (expected claude or codex)" >&2
      ;;
  esac

  # Session/thread markers prove the active harness. Config-directory env vars
  # are deliberately excluded: users commonly export them in every shell, so
  # they do not identify the process invoking FFS.
  if [ -n "${CODEX_SESSION_ID:-}" ] || [ -n "${CODEX_THREAD_ID:-}" ] \
     || [ -n "${CODEX_AGENT:-}" ] || [ -n "${CODEX_CI:-}" ]; then
    echo "codex"
  elif [ -n "${CLAUDE_SESSION_ID:-}" ] || [ -n "${CLAUDE_CODE:-}" ]; then
    echo "claude"
  elif [ "${FFS_HOST_PROCESS_DETECT:-on}" != "off" ] && command -v ps >/dev/null 2>&1; then
    # ponytail: bounded naive PPID walk (six hops); session markers above are
    # the primary signal. Inspect executable names, not full arguments, because
    # task data can itself contain the words "codex" or "claude".
    local pid="${PPID:-}" depth=0 line exe parent
    while [ -n "$pid" ] && [ "$pid" -gt 1 ] 2>/dev/null && [ "$depth" -lt 6 ]; do
      line="$(ps -p "$pid" -o comm= 2>/dev/null || true)"
      exe="${line%% *}"
      exe="${exe##*/}"
      case "$exe" in
        codex|codex-*) echo "codex"; return 0 ;;
        claude|claude-*) echo "claude"; return 0 ;;
      esac
      parent="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d '[:space:]')"
      [ -n "$parent" ] || break
      pid="$parent"
      depth=$((depth + 1))
    done
    echo "adversary-host: orchestrator undetected — defaulting to claude host" >&2
    echo "claude"
  else
    # stdout stays clean (callers capture stdout via $()) — note goes to stderr only.
    echo "adversary-host: orchestrator undetected — defaulting to claude host" >&2
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

cross_vendor_fallback_enabled() {
  case "${FFS_CROSS_VENDOR_FALLBACK:-on}" in
    0|false|off|no) return 1 ;;
    *) return 0 ;;
  esac
}

# adversary_invoke <kind> <timeout> <model> <effort> <prompt>
# Prints the underlying command's stdout+stderr and returns its exit code.
# effort is unused for kind=claude — accepted and ignored. rc=127 if the
# resolved bin is absent (fail-soft signal to the caller).
adversary_invoke() {
  local kind="$1" timeout_s="$2" model="$3" effort="$4" prompt="$5"
  if [ "$kind" = "codex" ]; then
    local bin="${ADVERSARY_BIN_CODEX:-codex}" last_message transcript rc
    if ! command -v "$bin" >/dev/null 2>&1; then
      return 127
    fi
    last_message="$(mktemp "${TMPDIR:-/tmp}/ffs-codex-last.XXXXXX")" || return 1
    transcript="$(mktemp "${TMPDIR:-/tmp}/ffs-codex-transcript.XXXXXX")" || {
      rm -f "$last_message"
      return 1
    }
    printf '%s' "$prompt" | run_bounded "$timeout_s" "$bin" exec \
      -c "model=\"$model\"" -c "model_reasoning_effort=\"$effort\"" \
      --sandbox read-only --ephemeral --ignore-user-config --ignore-rules \
      --color never --output-last-message "$last_message" \
      - >"$transcript" 2>&1
    rc="${PIPESTATUS[1]}"
    if [ "$rc" -eq 0 ] && [ -s "$last_message" ]; then
      # Codex's human transcript can repeat the final answer around hook and
      # token-usage output. Consume the CLI's canonical final-message channel.
      cat "$last_message"
    elif [ "$rc" -eq 0 ]; then
      # Compatibility for older/stub CLIs that accept or ignore -o without
      # materializing it. Their bounded transcript is the only payload.
      cat "$transcript"
    else
      cat "$transcript" >&2
    fi
    rm -f "$last_message" "$transcript"
    return "$rc"
  else
    local bin="${ADVERSARY_BIN_CLAUDE:-claude}"
    if ! command -v "$bin" >/dev/null 2>&1; then
      return 127
    fi
    # A review is data-only. Keep Claude non-agentic, non-persistent, and off
    # the consumer's ambient MCP/tool surface just as Codex is read-only.
    printf '%s' "$prompt" | run_bounded "$timeout_s" env \
      -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
      "$bin" --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
      --permission-mode plan --tools '' --no-session-persistence \
      -p --model "$model"
    rc="${PIPESTATUS[1]}"
    return "$rc"
  fi
}

# adversary_invoke_with_fallback <preferred-kind> <fallback-kind> <overall-timeout>
#   <preferred-model> <preferred-effort> <fallback-model> <fallback-effort>
#   <prompt>
# Read-only reviews may retry once across vendors within one overall deadline.
# The failed transcript is discarded and never fed to the fallback reviewer;
# only the fixed prompt is.
# Stateful GSD drives MUST NOT use this helper (gsd-run.sh selects pre-launch).
adversary_invoke_with_fallback() {
  local preferred="$1" fallback="$2" timeout_s="$3"
  local preferred_model="$4" preferred_effort="$5"
  local fallback_model="$6" fallback_effort="$7" prompt="$8"
  local output rc total primary_budget remaining start

  total="${timeout_s%%.*}"
  case "$total" in ''|*[!0-9]*|0) total=1 ;; esac
  # Split the advertised wall so even a dead preferred CLI leaves a reliable
  # fallback slice. A fast primary failure still gives the fallback more time.
  primary_budget=$(( total / 2 ))
  [ "$primary_budget" -ge 1 ] || primary_budget=1
  [ "$primary_budget" -le "$total" ] || primary_budget="$total"
  start="$SECONDS"

  output="$(adversary_invoke "$preferred" "$primary_budget" \
    "$preferred_model" "$preferred_effort" "$prompt" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '%s\n' "$output"
    return 0
  fi

  if ! cross_vendor_fallback_enabled; then
    echo "adversary-host: cross-vendor fallback disabled; returning $preferred failure rc=$rc" >&2
    return "$rc"
  fi

  echo "adversary-host: DEGRADED — $preferred reviewer unavailable (rc=$rc); trying one bounded $fallback fallback" >&2
  remaining=$(( total - (SECONDS - start) ))
  if [ "$remaining" -lt 1 ]; then
    echo "adversary-host: overall review deadline exhausted before fallback could start" >&2
    return 124
  fi
  output="$(adversary_invoke "$fallback" "$remaining" \
    "$fallback_model" "$fallback_effort" "$prompt" 2>&1)"
  rc=$?
  [ "$rc" -eq 0 ] && printf '%s\n' "$output"
  return "$rc"
}
