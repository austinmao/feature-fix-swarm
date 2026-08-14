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
#
# `${BASH_SOURCE[0]}` is EMPTY when this file is sourced from zsh, so
# `dirname ""` yielded "." and this resolved to ./run-bounded.sh -- which does
# not exist at the repo root. Measured under zsh:
#   scripts/gsd/adversary-host.sh:.:20: no such file or directory: ./run-bounded.sh
# `run_bounded` then stayed undefined and every bounded reviewer call failed.
# Resolved zsh-safely: $0 is the SOURCED FILE's path in zsh (verified), while
# bash populates BASH_SOURCE, so this one expression carries the right
# provenance in both -- and needs no zsh-only syntax such as ${(%):-%x}, which
# bash cannot even PARSE (so it cannot sit in an elif branch). Deliberately NOT
# falling back to the CWD's git root: sourcing one checkout's copy from another
# would silently load THAT checkout's run-bounded.sh, so a reviewer could run a
# run_bounded from a different revision than the branch under review, unwarned.
_ah_dir="$(dirname "${BASH_SOURCE[0]:-$0}")"
if [ ! -f "${_ah_dir}/run-bounded.sh" ]; then
  echo "adversary-host: FATAL: cannot locate run-bounded.sh next to adversary-host.sh (looked in '${_ah_dir}')" >&2
  unset _ah_dir
  return 1 2>/dev/null || exit 1
fi
. "${_ah_dir}/run-bounded.sh"
# Do not leak scaffolding into the caller's shell.
unset _ah_dir

adversary_model_request_helper() {
  local candidate
  for candidate in \
    "$(dirname "${BASH_SOURCE[0]:-$0}")/../../lib/model_requests.py" \
    "$(dirname "${BASH_SOURCE[0]:-$0}")/../../packages/feature-fix-swarm/lib/model_requests.py"; do
    if [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "adversary-host: typed model request resolver is unavailable" >&2
  return 78
}

# The evidence ledger is deliberately owned here: callers supply only seam/run
# context and cannot accidentally omit an attempt or invocation event.
adversary_gates() {
  local candidate
  for candidate in "$(dirname "${BASH_SOURCE[0]:-$0}")/../../lib/gates.py" "$(dirname "${BASH_SOURCE[0]:-$0}")/../../packages/feature-fix-swarm/lib/gates.py"; do
    [ -f "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
  done
  echo "adversary-host: gates.py unavailable" >&2
  return 78
}

adversary_evidence_enabled() {
  [ -n "${GSD_RUN_ID:-}" ] || [ -n "${GATES_STORE:-}" ]
}

adversary_note_rung() {
  local rung="$1" outcome="$2" gates before after
  adversary_evidence_enabled || return 0
  gates="$(adversary_gates)" || return $?
  before="$(python3 "$gates" note-degraded --tripped 2>/dev/null)" || return 78
  python3 "$gates" note-degraded rung-attempt --rung-id "$rung" --outcome "$outcome" >/dev/null || {
    echo "adversary-host: FATAL: durable rung evidence write failed for $rung" >&2; return 78; }
  after="$(python3 "$gates" note-degraded --tripped 2>/dev/null)" || return 78
  if ! printf '%s' "$before" | grep -Fq "\"$rung\"" && printf '%s' "$after" | grep -Fq "\"$rung\""; then
    echo "adversary-host: RUNG-TRIPPED $rung" >&2
  fi
}

adversary_tripped_rungs() {
  local gates
  adversary_evidence_enabled || { printf '[]'; return 0; }
  gates="$(adversary_gates)" || return $?
  python3 "$gates" note-degraded --tripped
}

adversary_record_invocation() {
  # Existing standalone helper users do not always have a ledger id. Real
  # review seams do; preserve historical library use while making supplied
  # context fail conspicuously if persistence is unavailable.
  [ -n "${GSD_RUN_ID:-}" ] || return 0
  local gates id
  gates="$(adversary_gates)" || return $?
  id="${ADVERSARY_INVOCATION_ID:-${ADVERSARY_SEAM:-adversary}-$$-$RANDOM}"
  python3 "$gates" note-degraded invocation --run-id "$GSD_RUN_ID" \
    --seam "${ADVERSARY_SEAM:-adversary}" --degraded "$1" --invocation-id "$id" >/dev/null || {
      echo "adversary-host: FATAL: durable invocation evidence write failed" >&2; return 78; }
}

# adversary_resolve_model_request <codex|claude> <request-json>
# Emits model|effort|kind. Raw vendor IDs are invalid input: callers expose
# only the typed JSON contract and the resolver owns host-specific IDs.
adversary_resolve_model_request() {
  local host="$1" request="$2" helper resolved
  helper="$(adversary_model_request_helper)" || return $?
  resolved="$(/usr/bin/python3 "$helper" resolve "$request" --host "$host")" || return $?
  printf '%s' "$resolved" | /usr/bin/python3 -c '
import json, re, sys
r = json.load(sys.stdin)
if r["kind"] == "exact" and not re.match(r"^(?:gpt-|o[1-9](?:-|$)|claude-)", r["model"]):
    print("adversary-host: exact model vendor is unsupported: {}".format(r["model"]), file=sys.stderr)
    raise SystemExit(2)
print("{}|{}|{}".format(r["model"], r.get("effort") or "", r["kind"]))
'
}

# adversary_invoke_typed_request <preferred-kind> <fallback-kind> <timeout>
#   <request-json> <prompt>
# Exact requests are provenance-sensitive: no model ladder and no cross-host
# fallback may satisfy them. Tier requests retain the bounded fallback policy.
adversary_invoke_typed_request() {
  local preferred="$1" fallback="$2" timeout_s="$3" request="$4" prompt="$5"
  local preferred_model preferred_effort request_kind fallback_model fallback_effort ignored exact_host
  IFS='|' read -r preferred_model preferred_effort request_kind <<EOF
$(adversary_resolve_model_request "$preferred" "$request")
EOF
  [ -n "$preferred_model" ] || return 78
  if [ "$request_kind" = exact ]; then
    case "$preferred_model" in
      gpt-*|o[1-9]*) exact_host=codex ;;
      claude-*) exact_host=claude ;;
      *)
        echo "adversary-host: exact model vendor is unsupported: $preferred_model" >&2
        return 2
        ;;
    esac
    IFS='|' read -r preferred_model preferred_effort ignored <<EOF
$(adversary_resolve_model_request "$exact_host" "$request")
EOF
    : "$ignored"
    adversary_invoke_model_ladder "$exact_host" "$timeout_s" "$preferred_model" "$preferred_effort" "$prompt" "" "" "$preferred_model|$preferred_effort"
    local exact_rc=$?
    # Record only a SUCCESSFUL invocation (mirrors the fallback path): a
    # failed exact invocation recorded as degraded=false would inflate the
    # denominator and dilute degraded_ratio() — the REQ-102 gate's input.
    if [ "$exact_rc" -eq 0 ]; then
      adversary_record_invocation false || return $?
    fi
    return "$exact_rc"
  fi
  IFS='|' read -r fallback_model fallback_effort ignored <<EOF
$(adversary_resolve_model_request "$fallback" "$request")
EOF
  : "$ignored"
  [ -n "$fallback_model" ] || return 78
  adversary_invoke_with_fallback "$preferred" "$fallback" "$timeout_s" \
    "$preferred_model" "$preferred_effort" "$fallback_model" "$fallback_effort" "$prompt"
}

adversary_reject_legacy_model_vars() {
  local name
  for name in "$@"; do
    if [ "${!name+x}" = x ]; then
      echo "adversary-host: $name is unsupported; use the caller's typed *_MODEL_REQUEST JSON variable" >&2
      return 2
    fi
  done
}

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
    case "$preferred" in *sol*) ;; *) printf '%s|%s\n' gpt-5.6-sol high ;; esac
    case "$preferred" in *terra*) ;; *) printf '%s|%s\n' gpt-5.6-terra medium ;; esac
    case "$preferred" in *luna*) ;; *) printf '%s|%s\n' gpt-5.6-luna low ;; esac
  else
    case "$preferred" in *opus*) ;; *) printf '%s|\n' claude-opus-5 ;; esac
    case "$preferred" in *sonnet*) ;; *) printf '%s|\n' claude-sonnet-5 ;; esac
    case "$preferred" in *haiku*) ;; *) printf '%s|\n' claude-haiku-4-5-20251001 ;; esac
  fi
}

# adversary_invoke <kind> <timeout> <model> <effort> <prompt> [schema_file]
# Prints the underlying command's stdout+stderr and returns its exit code.
# effort is unused for kind=claude — accepted and ignored. rc=127 if the
# resolved bin is absent (fail-soft signal to the caller).
#
# AC-016(a): optional trailing schema_file — codex natively enforces it via
# --output-schema (structured-output support in the pinned CLI); claude has
# no equivalent flag, so schema_file is accepted-and-ignored on that branch
# (post-hoc jq-validation is the caller's job in adversary_invoke_model_ladder,
# which applies uniformly to both hosts). Absent schema_file = byte-identical
# to the pre-AC-016 invocation.
adversary_invoke() {
  local kind="$1" timeout_s="$2" model="$3" effort="$4" prompt="$5" schema_file="${6:-}"
  if [ "$kind" = "codex" ]; then
    local bin="${ADVERSARY_BIN_CODEX:-codex}" last_message transcript data_dir rc
    if ! command -v "$bin" >/dev/null 2>&1; then
      return 127
    fi
    last_message="$(mktemp "${TMPDIR:-/tmp}/ffs-codex-last.XXXXXX")" || return 1
    transcript="$(mktemp "${TMPDIR:-/tmp}/ffs-codex-transcript.XXXXXX")" || {
      rm -f "$last_message"
      return 1
    }
    data_dir="$(mktemp -d "${TMPDIR:-/tmp}/ffs-codex-data-only.XXXXXX")" || {
      rm -f "$last_message" "$transcript"
      return 1
    }
    local -a schema_args=()
    [ -n "$schema_file" ] && schema_args=(--output-schema "$schema_file")
    # Codex has no Claude-style `--tools ''` flag. Disable every built-in
    # agentic tool family supported by the pinned CLI, run from a disposable
    # empty directory, and retain the read-only sandbox as defense in depth.
    # Provider overrides are stripped so an adversarial review cannot switch
    # away from first-party subscription auth or inherit a metered API key.
    printf '%s' "$prompt" | run_bounded "$timeout_s" /usr/bin/env \
      -u OPENAI_API_KEY -u OPENAI_BASE_URL -u OPENAI_API_BASE \
      -u CODEX_MODEL_PROVIDER \
      "$bin" exec \
      -c "model=\"$model\"" -c "model_reasoning_effort=\"$effort\"" \
      --sandbox read-only --ephemeral --ignore-user-config --ignore-rules \
      --disable shell_tool --disable unified_exec \
      --disable code_mode --disable code_mode_host \
      --disable apps --disable browser_use --disable computer_use \
      --disable js_repl --disable multi_agent --disable multi_agent_v2 \
      --disable image_generation \
      -C "$data_dir" --skip-git-repo-check \
      --color never --output-last-message "$last_message" \
      "${schema_args[@]}" \
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
    rm -rf "$data_dir"
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
#   [per-attempt-cap] [review-cap] [ordered-rungs] [schema-file] [validate-cmd]
# Try models on one vendor before crossing vendors. Each attempt has its own
# wall-clock cap; rc=124 means the CLI itself is hanging, so lower models on
# that same binary are skipped. Failed transcripts are intentionally discarded
# and never become input to a later reviewer.
#
# AC-016(b) ordered-rungs: when non-empty, REPLACES the built-in
# adversary_model_ladder sequence with the caller's own newline-separated
# "model|effort" list (same wire shape adversary_model_ladder emits) — the
# plan-wall selection algorithm needs fable-first (Claude fence case) or
# terra-first (Codex distinct-model rule) orderings the built-in ladder can
# never produce (it always ladders down FROM the preferred model). Absent =
# byte-identical to the pre-AC-016 built-in ladder.
#
# AC-016(a) schema validation: when both schema-file and validate-cmd are
# non-empty, a rung that returns rc=0 is additionally piped through
# `"$validate_cmd" "$schema_file"` (candidate output on stdin); a nonzero
# exit there is treated as a RUNG FAILURE (ladder continues), never a caller
# error — invalid/empty output must never look like a successful review.
adversary_invoke_model_ladder() {
  local kind="$1" timeout_s="$2" preferred_model="$3"
  local preferred_effort="$4" prompt="$5" requested_cap="${6:-}"
  local requested_review_cap="${7:-}" ordered_rungs="${8:-}"
  local schema_file="${9:-}" validate_cmd="${10:-}"
  local total attempt_cap started remaining budget model effort output rc
  local probe_enabled probe_timeout probe_output review_cap rung_source tripped rung candidates all_tripped probe_due

  total="${timeout_s%%.*}"
  case "$total" in ''|*[!0-9]*|0) total=1 ;; esac
  attempt_cap="${requested_cap:-${FFS_ADVERSARY_ATTEMPT_TIMEOUT:-120}}"
  case "$attempt_cap" in ''|*[!0-9]*|0) attempt_cap=120 ;; esac
  probe_enabled="${FFS_ADVERSARY_MODEL_PROBE:-on}"
  probe_timeout="${FFS_ADVERSARY_MODEL_PROBE_TIMEOUT:-60}"
  case "$probe_timeout" in ''|*[!0-9]*|0) probe_timeout=60 ;; esac
  review_cap="${requested_review_cap:-${FFS_ADVERSARY_REVIEW_ATTEMPT_TIMEOUT:-180}}"
  case "$review_cap" in ''|*[!0-9]*|0) review_cap=180 ;; esac
  started="$SECONDS"
  if [ -n "$ordered_rungs" ]; then
    rung_source="$ordered_rungs"
  else
    rung_source="$(adversary_model_ladder "$kind" "$preferred_model" "$preferred_effort")"
  fi

  # The tripped-rung filter is applied IN the attempt loop below (skip unless
  # PROBE-DUE), never by removing rungs from the iterated ladder: a pre-filter
  # that drops tripped rungs makes the half-open probe unreachable whenever
  # any untripped sibling exists, so a tripped rung could never recover
  # (review-gate finding, 2026-08-09). This count only decides all_tripped:
  # when every option is tripped, fail-closed review outranks cadence and the
  # ladder runs as normal fallback attempts, never rate-limited probes.
  tripped="$(adversary_tripped_rungs)" || return $?
  candidates=0
  while IFS='|' read -r model effort; do
    [ -n "$model" ] || continue
    rung="$kind:$model:${effort:--}"
    if ! printf '%s' "$tripped" | grep -Fq "\"$rung\""; then
      candidates=$((candidates + 1))
    fi
  done <<EOF
$rung_source
EOF
  all_tripped=0
  [ "$candidates" -gt 0 ] || all_tripped=1

  while IFS='|' read -r model effort; do
    [ -n "$model" ] || continue
    rung="$kind:$model:${effort:--}"
    if [ "$all_tripped" -eq 0 ] && printf '%s' "$tripped" | grep -Fq "\"$rung\""; then
      probe_due="$(python3 "$(adversary_gates)" note-degraded --probe-check "$rung" 2>/dev/null)" || return 78
      [ "$probe_due" = PROBE-DUE ] || continue
    fi
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
        adversary_note_rung "$rung" fail || return $?
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

    output="$(adversary_invoke "$kind" "$budget" "$model" "$effort" "$prompt" "$schema_file" 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$schema_file" ] && [ -n "$validate_cmd" ]; then
      if ! printf '%s' "$output" | "$validate_cmd" "$schema_file" >/dev/null 2>&1; then
        echo "adversary-host: $kind model $model rung output failed schema validation ($schema_file)" >&2
        rc=97   # sentinel: schema-invalid — distinct from CLI-unavailable, still a rung failure
      fi
    fi
    if [ "$rc" -eq 0 ]; then
      adversary_note_rung "$rung" ok || return $?
      if [ "$model" != "$preferred_model" ]; then
        echo "adversary-host: MODEL_FALLBACK — $kind $preferred_model unavailable; selected $model" >&2
      fi
      echo "adversary-host: SELECTED $kind $model ${effort:--}"
      printf '%s\n' "$output"
      return 0
    fi
    adversary_note_rung "$rung" fail || return $?
    echo "adversary-host: $kind model $model unavailable (rc=$rc)" >&2
    # With an admission probe, a review timeout may be model-specific; try the
    # next candidate if deadline remains. Without a probe, preserve the older
    # dead-CLI inference and cross vendors immediately.
    if [ "$rc" -eq 124 ] && [ "$probe_enabled" = "off" ]; then
      return 124
    fi
  done <<EOF
$rung_source
EOF
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
  # Split the advertised wall evenly so a healthy preferred CLI has enough
  # time for a substantive diff while a dead CLI still leaves a complete
  # fallback slice. The previous one-quarter share admitted Claude on a tiny
  # probe, then killed every real 90-100 KB review before it could answer.
  primary_budget=$(( total / 2 ))
  [ "$primary_budget" -ge 1 ] || primary_budget=1
  [ "$primary_budget" -le "$total" ] || primary_budget="$total"
  start="$SECONDS"

  output="$(adversary_invoke_model_ladder "$preferred" "$primary_budget" \
    "$preferred_model" "$preferred_effort" "$prompt" \
    "${FFS_ADVERSARY_PREFERRED_ATTEMPT_TIMEOUT:-45}" \
    "${FFS_ADVERSARY_PREFERRED_REVIEW_TIMEOUT:-90}" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    adversary_record_invocation false || return $?
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
  if [ "$rc" -eq 0 ]; then
    adversary_record_invocation true || return $?
    printf '%s\n' "$output"
  fi
  return "$rc"
}
