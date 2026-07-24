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

# Emit a quality-descending, de-duplicated model ladder for one vendor. The
# requested model always stays first; lower/alternate tiers are admission
# fallbacks, never silent substitutions. This lets a live CLI with a
# quota-limited model stay on the invoking host instead of needlessly crossing
# vendors (or declaring every host unavailable).
adversary_model_ladder() {
  local kind="$1" preferred="$2" preferred_effort="$3"
  printf '%s|%s\n' "$preferred" "$preferred_effort"
  if [ "$kind" = "codex" ]; then
    case "$preferred" in *sol*) ;; *) printf '%s|%s\n' gpt-5.6-sol xhigh ;; esac
    case "$preferred" in *terra*) ;; *) printf '%s|%s\n' gpt-5.6-terra high ;; esac
    case "$preferred" in *luna*) ;; *) printf '%s|%s\n' gpt-5.6-luna medium ;; esac
  else
    case "$preferred" in *opus*) ;; *) printf '%s|\n' claude-opus-5 ;; esac
    case "$preferred" in *sonnet*) ;; *) printf '%s|\n' claude-sonnet-5 ;; esac
    case "$preferred" in *haiku*) ;; *) printf '%s|\n' claude-haiku-4-5-20251001 ;; esac
  fi
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

# adversary_invoke_model_ladder <kind> <timeout> <model> <effort> <prompt>
#   [per-attempt-cap] [review-cap]
# Try models on one vendor before crossing vendors. Each attempt has its own
# wall-clock cap; rc=124 means the CLI itself is hanging, so lower models on
# that same binary are skipped. Failed transcripts are intentionally discarded
# and never become input to a later reviewer.
adversary_invoke_model_ladder() {
  local kind="$1" timeout_s="$2" preferred_model="$3"
  local preferred_effort="$4" prompt="$5" requested_cap="${6:-}"
  local requested_review_cap="${7:-}"
  local total attempt_cap started remaining budget model effort output rc
  local probe_enabled probe_timeout probe_output review_cap

  total="${timeout_s%%.*}"
  case "$total" in ''|*[!0-9]*|0) total=1 ;; esac
  attempt_cap="${requested_cap:-${FFS_ADVERSARY_ATTEMPT_TIMEOUT:-120}}"
  case "$attempt_cap" in ''|*[!0-9]*|0) attempt_cap=120 ;; esac
  probe_enabled="${FFS_ADVERSARY_MODEL_PROBE:-on}"
  probe_timeout="${FFS_ADVERSARY_MODEL_PROBE_TIMEOUT:-20}"
  case "$probe_timeout" in ''|*[!0-9]*|0) probe_timeout=20 ;; esac
  review_cap="${requested_review_cap:-${FFS_ADVERSARY_REVIEW_ATTEMPT_TIMEOUT:-180}}"
  case "$review_cap" in ''|*[!0-9]*|0) review_cap=180 ;; esac
  started="$SECONDS"

  while IFS='|' read -r model effort; do
    remaining=$(( total - (SECONDS - started) ))
    [ "$remaining" -ge 1 ] || return 124
    if [ "$probe_enabled" != "off" ]; then
      budget="$probe_timeout"
      [ "$budget" -le "$remaining" ] || budget="$remaining"
      probe_output="$(adversary_invoke "$kind" "$budget" "$model" "$effort" \
        'This is a read-only model availability probe. Use no tools. Output exactly: FFS_ADVERSARY_MODEL_READY' 2>&1)"
      rc=$?
      if [ "$rc" -ne 0 ] || ! printf '%s\n' "$probe_output" | grep -qx 'FFS_ADVERSARY_MODEL_READY'; then
        [ "$rc" -ne 0 ] || rc=1
        echo "adversary-host: $kind model $model probe unavailable (rc=$rc)" >&2
        # A model probe timeout is not a host verdict: usage limits can make
        # one Codex/Claude tier hang while another tier on the same CLI works.
        # Continue the bounded ladder; at worst this spends one short probe
        # window per model before crossing vendors.
        continue
      fi
      remaining=$(( total - (SECONDS - started) ))
      [ "$remaining" -ge 1 ] || return 124
      budget="$review_cap"
      [ "$budget" -le "$remaining" ] || budget="$remaining"
    else
      # Hermetic compatibility seam for tests/forensics: invoke the actual
      # review directly, still bounded per attempt.
      budget="$attempt_cap"
      [ "$budget" -le "$remaining" ] || budget="$remaining"
    fi

    output="$(adversary_invoke "$kind" "$budget" "$model" "$effort" "$prompt" 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      if [ "$model" != "$preferred_model" ]; then
        echo "adversary-host: MODEL_FALLBACK — $kind $preferred_model unavailable; selected $model" >&2
      fi
      echo "adversary-host: SELECTED $kind $model ${effort:--}"
      printf '%s\n' "$output"
      return 0
    fi
    echo "adversary-host: $kind model $model unavailable (rc=$rc)" >&2
    # With an admission probe, a review timeout may be model-specific; try the
    # next candidate if deadline remains. Without a probe, preserve the older
    # dead-CLI inference and cross vendors immediately.
    if [ "$rc" -eq 124 ] && [ "$probe_enabled" = "off" ]; then
      return 124
    fi
  done < <(adversary_model_ladder "$kind" "$preferred_model" "$preferred_effort")
  return "${rc:-1}"
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
  # Opposite-vendor review remains preferred, but it must not consume half the
  # whole deadline when a full-context request is quota-limited even though a
  # tiny availability probe succeeded. Reserve the majority for fallback.
  primary_budget=$(( total / 4 ))
  [ "$primary_budget" -ge 1 ] || primary_budget=1
  [ "$primary_budget" -le "$total" ] || primary_budget="$total"
  start="$SECONDS"

  output="$(adversary_invoke_model_ladder "$preferred" "$primary_budget" \
    "$preferred_model" "$preferred_effort" "$prompt" \
    "${FFS_ADVERSARY_PREFERRED_ATTEMPT_TIMEOUT:-45}" \
    "${FFS_ADVERSARY_PREFERRED_REVIEW_TIMEOUT:-90}" 2>&1)"
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
  output="$(adversary_invoke_model_ladder "$fallback" "$remaining" \
    "$fallback_model" "$fallback_effort" "$prompt" \
    "${FFS_ADVERSARY_FALLBACK_ATTEMPT_TIMEOUT:-120}" \
    "${FFS_ADVERSARY_FALLBACK_REVIEW_TIMEOUT:-240}" 2>&1)"
  rc=$?
  [ "$rc" -eq 0 ] && printf '%s\n' "$output"
  return "$rc"
}
