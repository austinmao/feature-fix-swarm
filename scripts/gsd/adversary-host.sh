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
_ADVERSARY_HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$_ADVERSARY_HOST_DIR/run-bounded.sh"
. "$_ADVERSARY_HOST_DIR/model-equivalents.sh"

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

alternate_vendor_kind() {
  case "${1:-}" in
    codex) echo "claude" ;;
    claude) echo "codex" ;;
    *) return 2 ;;
  esac
}

cross_vendor_fallback_enabled() {
  case "${FFS_CROSS_VENDOR_FALLBACK:-on}" in
    0|false|off|no) return 1 ;;
    *) return 0 ;;
  esac
}

# vendor_failure_is_unavailable <rc> <captured-stderr-file> [review|mutating]
#
# Mutating callers classify only a separately captured stderr diagnostic
# channel. Model/task stdout is untrusted data: a task that happens to say
# "usage limit" or "connection refused" must never manufacture a replay.
# Read-only reviewers may additionally classify stdout because current Claude
# builds emit subscription/session-limit diagnostics there; a false positive
# can at worst run one extra bounded read-only review, never duplicate writes.
#
# Read-only reviews may retry after a bounded timeout. Mutating drives may not:
# rc=124 says only that the process was reaped, not whether it changed local or
# external state before hanging. They fail promptly and let the caller resume
# from durable state instead of starting an ambiguous duplicate execution.
vendor_failure_is_unavailable() {
  local rc="${1:-1}" diagnostics_file="${2:-}" mode="${3:-review}"
  case "$rc" in
    126|127) return 0 ;;
    124) [ "$mode" = "review" ]; return ;;
  esac
  [ -f "$diagnostics_file" ] || return 1

  # Exact CLI/provider diagnostics only. Keep this list intentionally narrower
  # than natural-language task failures; transport tokens are protocol/runtime
  # identifiers, not prose such as "connection refused".
  LC_ALL=C grep -Eiq \
    "(You've hit your (usage|session) limit|usage limit[^[:cntrl:]]*(reset|reached|exceeded)|session limit[^[:cntrl:]]*(reset|reached|exceeded)|rate limit (reached|exceeded)|insufficient_quota|quota (has been )?(reached|exceeded|exhausted)|credits? exhausted|model[^[:cntrl:]]*(not found|unavailable|unsupported|does not exist)|no available model|no capacity|overloaded_error|server is overloaded|temporarily unavailable|service unavailable|authentication (failed|required)|not logged in|login required|(oauth|access|authentication) token[^[:cntrl:]]*expired|(^|[^0-9])(401|403|429|503|529)([^0-9]|$)|(ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENETUNREACH|EAI_AGAIN)|stream (disconnected|closed before)|DNS (lookup|resolution) failed|TLS handshake failed)" \
    "$diagnostics_file"
}

_adversary_invoke_once() {
  local kind="$1" timeout_s="$2" model="$3" effort="$4" prompt="$5"
  if [ "$kind" = "codex" ]; then
    local bin="${ADVERSARY_BIN_CODEX:-codex}"
    if ! command -v "$bin" >/dev/null 2>&1; then
      echo "adversary-host: Codex CLI not found ($bin)" >&2
      return 127
    fi
    # Feed review data through stdin. Passing a large diff as one argv entry can
    # exceed ARG_MAX and silently skip the independent review.
    printf '%s' "$prompt" | run_bounded "$timeout_s" "$bin" exec \
      -c "model=\"$model\"" -c "model_reasoning_effort=\"$effort\"" \
      -c 'sandbox_mode="read-only"' \
      -
    return $?
  fi

  local bin="${ADVERSARY_BIN_CLAUDE:-claude}"
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "adversary-host: Claude CLI not found ($bin)" >&2
    return 127
  fi
  # Claude has no sandbox flag equivalent to Codex. Enforce a model-data-only
  # review by disabling built-in tools, MCP, ordinary project/user
  # customizations and hooks, and session persistence. Administrator-managed
  # policy (including managed hooks) intentionally remains in force: that is a
  # trusted host boundary which FFS must not bypass, not model-controlled code.
  # The model receives only the prompt streamed on stdin.
  printf '%s' "$prompt" | run_bounded "$timeout_s" env \
    -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
    "$bin" --safe-mode --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
    --tools "" --permission-mode dontAsk --no-session-persistence \
    -p --model "$model"
}

# adversary_invoke <kind> <timeout> <model> <effort> <prompt>
# Prints the underlying command's stdout+stderr and returns its exit code.
# effort is unused for kind=claude — accepted and ignored. The requested
# (normally opposite-host) vendor runs first. Availability failures make one
# bounded attempt on the other vendor; ordinary reviewer/process failures do
# not cross. Set FFS_CROSS_VENDOR_FALLBACK=off for forensic isolation.
adversary_invoke() {
  local kind="$1" timeout_s="$2" model="$3" effort="$4" prompt="$5"
  local primary_out primary_err primary_rc primary_unavailable=false
  local fallback_kind fallback_model fallback_effort
  primary_out="$(mktemp "${TMPDIR:-/tmp}/ffs-adversary-out.XXXXXX")" || return 1
  primary_err="$(mktemp "${TMPDIR:-/tmp}/ffs-adversary-err.XXXXXX")" || {
    rm -f "$primary_out"
    return 1
  }

  _adversary_invoke_once "$kind" "$timeout_s" "$model" "$effort" "$prompt" \
    >"$primary_out" 2>"$primary_err"
  primary_rc=$?
  cat "$primary_out"
  cat "$primary_err" >&2
  if [ "$primary_rc" -eq 0 ]; then
    rm -f "$primary_out" "$primary_err"
    return 0
  fi

  if vendor_failure_is_unavailable "$primary_rc" "$primary_err" review \
     || vendor_failure_is_unavailable "$primary_rc" "$primary_out" review; then
    primary_unavailable=true
  fi
  if ! cross_vendor_fallback_enabled || [ "$primary_unavailable" != true ]; then
    rm -f "$primary_out" "$primary_err"
    return "$primary_rc"
  fi
  rm -f "$primary_out" "$primary_err"

  fallback_kind="$(alternate_vendor_kind "$kind")" || return "$primary_rc"
  if [ "$fallback_kind" = "codex" ]; then
    fallback_model="$(codex_equiv_model "$model")" || fallback_model="gpt-5.6-sol"
    fallback_effort="$(codex_equiv_effort "$model")" || fallback_effort="xhigh"
  else
    fallback_model="$(claude_equiv_model "$model")" || fallback_model="opus"
    fallback_effort=""
  fi
  echo "adversary-host: $kind reviewer unavailable (rc=$primary_rc); falling back once to $fallback_kind" >&2
  _adversary_invoke_once "$fallback_kind" "$timeout_s" \
    "$fallback_model" "$fallback_effort" "$prompt"
}
