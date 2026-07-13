#!/usr/bin/env bash
# Host-native headless GSD runner. A fixed, read-only capability probe chooses
# a usable vendor BEFORE the stateful drive starts. Once started, a drive is
# never replayed on another vendor: nonzero/timeout returns resume guidance.
# Usage: gsd-run.sh <slash-command> [args...]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/adversary-host.sh"
. "$SCRIPT_DIR/model-equivalents.sh"

if [ $# -lt 1 ]; then
  echo "usage: gsd-run.sh <slash-command> [args...]" >&2
  exit 2
fi

case "$1" in
  /gsd-*) GSD_SKILL_NAME="${1#/}" ;;
  \$gsd-*) GSD_SKILL_NAME="${1#\$}" ;;
  *) echo "gsd-run: unsupported GSD command: $1" >&2; exit 2 ;;
esac

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || exit 1

# gsd's mempalace commands call a bare `mempalace` binary in headless mode.
export PATH="$REPO_ROOT/scripts/gsd:$PATH"

TIMEOUT_SECS="${TIMEOUT:-900}"
PROBE_TIMEOUT_SECS="${GSD_HOST_PROBE_TIMEOUT:-45}"
PROBE_MARKER="FFS_HOST_PROBE_READY"
PROBE_PROMPT="This is a read-only availability probe. Use no tools. Output exactly: $PROBE_MARKER"
LOG_DIR="$REPO_ROOT/.planning/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/gsd-run-${TS}.log"

ACTIVE_HOST="$(detect_orchestrator_host)" || exit $?
LEAD_TIER="${GSD_LEAD_MODEL:-sonnet}"
CODEX_RUNTIME_HOME=""
CODEX_SESSION_CONTRACT="${GSD_CODEX_SESSION_CONTRACT:-FFS CODEX EXEC-SESSION CONTRACT: A tool result saying 'Script running with cell ID' is not completion or failure. Wait on that yielded cell. If the wait result contains a session_id and no exit_code, the nested process is still alive: poll that exact session with write_stdin until it exits. Never launch a replacement command while the original session is alive.}"

cleanup_codex_runtime() {
  if [ -n "$CODEX_RUNTIME_HOME" ] && [ -d "$CODEX_RUNTIME_HOME" ]; then
    rm -rf "$CODEX_RUNTIME_HOME"
  fi
}
trap cleanup_codex_runtime EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

host_label() {
  case "$1" in codex) echo Codex ;; claude) echo Claude ;; esac
}

alternate_host() {
  case "$1" in codex) echo claude ;; claude) echo codex ;; esac
}

codex_lead_model() {
  codex_equiv_model "$LEAD_TIER" 2>/dev/null || printf '%s\n' "$LEAD_TIER"
}

codex_lead_effort() {
  local effort
  effort="$(codex_equiv_effort "$LEAD_TIER" 2>/dev/null || true)"
  printf '%s\n' "${GSD_LEAD_EFFORT:-${effort:-high}}"
}

claude_lead_model() {
  local model
  model="$(claude_equiv_model "$LEAD_TIER" 2>/dev/null || true)"
  if [ -z "$model" ] || [ "$model" = "$LEAD_TIER" ]; then
    case "$LEAD_TIER" in
      sonnet|claude-sonnet-*) model="claude-sonnet-5" ;;
      opus|claude-opus-*) model="claude-opus-4-8" ;;
      fable|claude-fable-*) model="claude-fable-5" ;;
      haiku|claude-haiku-*) model="claude-haiku-4-5-20251001" ;;
      *) model="$LEAD_TIER" ;;
    esac
  fi
  printf '%s\n' "$model"
}

record_probe_note() {
  printf 'gsd-run: %s\n' "$1" >&2
  printf 'gsd-run: %s\n' "$1" >> "$LOG_FILE"
}

record_probe_output() {
  local kind="$1" rc="$2" raw="$3" limit bytes sanitized truncated=""
  limit="${GSD_PROBE_LOG_LIMIT:-4096}"
  case "$limit" in ''|*[!0-9]*|0) limit=4096 ;; esac
  bytes="$(printf '%s' "$raw" | wc -c | tr -d '[:space:]')"
  sanitized="$(printf '%s' "$raw" | LC_ALL=C head -c "$limit" | sed -E \
    -e 's#https?://[^[:space:]]+#[REDACTED_URL]#g' \
    -e 's#([Bb]earer[[:space:]]+)[^[:space:]]+#\1[REDACTED]#g' \
    -e 's#([A-Za-z_]*(TOKEN|SECRET|PASSWORD|PASS|KEY|AUTH|CREDENTIAL)[A-Za-z_]*[=:])[[:space:]]*[^[:space:]]+#\1[REDACTED]#g' \
    -e 's#[A-Za-z0-9_+/=-]{24,}#[REDACTED]#g')"
  [ "$bytes" -le "$limit" ] || truncated=" [truncated from ${bytes} bytes]"
  {
    printf 'gsd-run: %s probe output (rc=%s, redacted, max=%s bytes)%s\n' \
      "$(host_label "$kind")" "$rc" "$limit" "$truncated"
    [ -z "$sanitized" ] || printf '%s\n' "$sanitized"
  } >&2
  {
    printf 'gsd-run: %s probe output (rc=%s, redacted, max=%s bytes)%s\n' \
      "$(host_label "$kind")" "$rc" "$limit" "$truncated"
    [ -z "$sanitized" ] || printf '%s\n' "$sanitized"
  } >> "$LOG_FILE"
}

codex_source_root() {
  if [ -n "${GSD_CODEX_CONFIG_ROOT:-}" ]; then
    printf '%s\n' "$GSD_CODEX_CONFIG_ROOT"
    return 0
  fi
  if [ -f "$REPO_ROOT/.codex/skills/$GSD_SKILL_NAME/SKILL.md" ] \
     && find "$REPO_ROOT/.codex/agents" -maxdepth 1 -name 'gsd-*.toml' \
        -print -quit 2>/dev/null | grep -q .; then
    printf '%s\n' "$REPO_ROOT/.codex"
  else
    printf '%s\n' "${CODEX_HOME:-$HOME/.codex}"
  fi
}

command_surface_available() {
  local kind="$1" root
  if [ "$kind" = "codex" ]; then
    root="$(codex_source_root)"
    if [ ! -f "$root/skills/$GSD_SKILL_NAME/SKILL.md" ]; then
      echo "gsd-run: exact $GSD_SKILL_NAME surface unavailable for Codex at $root/skills/$GSD_SKILL_NAME/SKILL.md" >&2
      return 78
    fi
    if ! find "$root/agents" -maxdepth 1 -name 'gsd-*.toml' -print -quit \
         2>/dev/null | grep -q .; then
      echo "gsd-run: exact $GSD_SKILL_NAME surface unavailable for Codex: no gsd-*.toml agents under $root/agents" >&2
      return 78
    fi
  else
    root="${GSD_CLAUDE_SKILLS_ROOT:-$HOME/.claude/skills}"
    if [ ! -f "$root/$GSD_SKILL_NAME/SKILL.md" ]; then
      echo "gsd-run: exact $GSD_SKILL_NAME surface unavailable for Claude at $root/$GSD_SKILL_NAME/SKILL.md" >&2
      return 78
    fi
  fi
}

prepare_codex_runtime() {
  local source_root source_skill agent copied=0 auth_source
  source_root="$(codex_source_root)"
  command_surface_available codex || return $?
  CODEX_RUNTIME_HOME="$(mktemp -d "${TMPDIR:-/tmp}/ffs-gsd-codex.XXXXXX")" || return 1
  mkdir -p "$CODEX_RUNTIME_HOME/skills" "$CODEX_RUNTIME_HOME/agents"
  source_skill="$source_root/skills/$GSD_SKILL_NAME"
  cp -R "$source_skill" "$CODEX_RUNTIME_HOME/skills/$GSD_SKILL_NAME"
  for agent in "$source_root"/agents/gsd-*.toml; do
    [ -f "$agent" ] || continue
    cp "$agent" "$CODEX_RUNTIME_HOME/agents/"
    copied=$((copied + 1))
  done
  if [ "$copied" -eq 0 ]; then
    echo "gsd-run: no GSD Codex agents copied into isolated runtime" >&2
    return 78
  fi
  {
    echo 'approval_policy = "never"'
    echo 'sandbox_mode = "workspace-write"'
    echo '[features]'
    echo 'multi_agent = true'
  } > "$CODEX_RUNTIME_HOME/config.toml"
  auth_source="$source_root/auth.json"
  if [ ! -e "$auth_source" ]; then
    auth_source="${CODEX_HOME:-$HOME/.codex}/auth.json"
  fi
  [ ! -e "$auth_source" ] || ln -s "$auth_source" "$CODEX_RUNTIME_HOME/auth.json"
  if [ -x "$SCRIPT_DIR/codex-model-sync.sh" ]; then
    "$SCRIPT_DIR/codex-model-sync.sh" "$CODEX_RUNTIME_HOME" || {
      echo "gsd-run: Codex model materialization failed before the drive started" >&2
      return 1
    }
  fi
}

# A probe is deliberately independent of task text and read-only. Any failure
# here means the host cannot be admitted for a new drive; no output-substring
# classification is used. The stateful invocation below is a separate call.
probe_host() {
  local kind="$1" bin output rc model effort
  if [ "$kind" = "codex" ]; then
    bin="${CODEX_BIN:-codex}"
    command -v "$bin" >/dev/null 2>&1 || return 127
    model="$(codex_lead_model)"
    effort="$(codex_lead_effort)"
    output="$(run_bounded "$PROBE_TIMEOUT_SECS" "$bin" exec \
      -c "model=\"$model\"" -c "model_reasoning_effort=\"$effort\"" \
      --sandbox read-only --ephemeral --ignore-user-config --ignore-rules \
      --color never "$PROBE_PROMPT" </dev/null 2>&1)"
    rc=$?
  else
    bin="${CLAUDE_BIN:-claude}"
    command -v "$bin" >/dev/null 2>&1 || return 127
    model="$(claude_lead_model)"
    output="$(run_bounded "$PROBE_TIMEOUT_SECS" env \
      -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
      "$bin" --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
      --permission-mode plan --tools '' --no-session-persistence \
      --model "$model" -p "$PROBE_PROMPT" </dev/null 2>&1)"
    rc=$?
  fi
  record_probe_output "$kind" "$rc" "$output"
  [ "$rc" -eq 0 ] || return "$rc"
  if ! printf '%s\n' "$output" | grep -qx "$PROBE_MARKER"; then
    record_probe_note "$(host_label "$kind") probe missing acknowledgement '$PROBE_MARKER'"
    return 1
  fi
  command_surface_available "$kind"
}

SELECTED_HOST="$ACTIVE_HOST"
probe_host "$ACTIVE_HOST"
_native_rc=$?
if [ "$_native_rc" -ne 0 ]; then
  if ! cross_vendor_fallback_enabled; then
    record_probe_note "cross-vendor fallback disabled; native $(host_label "$ACTIVE_HOST") probe failed rc=$_native_rc and no stateful drive started"
    exit "$_native_rc"
  fi
  ALTERNATE_HOST="$(alternate_host "$ACTIVE_HOST")"
  echo "gsd-run: native $(host_label "$ACTIVE_HOST") unavailable before launch (probe rc=$_native_rc); checking $(host_label "$ALTERNATE_HOST")" >&2
  probe_host "$ALTERNATE_HOST"
  _alternate_rc=$?
  if [ "$_alternate_rc" -eq 0 ]; then
    SELECTED_HOST="$ALTERNATE_HOST"
    echo "gsd-run: DEGRADED — selected $(host_label "$SELECTED_HOST") before launch; the drive will run once on that host" >&2
  else
    echo "gsd-run: no usable host before launch (native rc=$_native_rc, alternate rc=$_alternate_rc); no stateful drive started" >&2
    echo "gsd-run: restore either CLI/model quota, then resume this exact GSD command" >&2
    exit 69
  fi
fi

first="$1"
shift
if [ "$SELECTED_HOST" = "codex" ]; then
  CODEX_BIN="${CODEX_BIN:-codex}"
  LEAD_MODEL="$(codex_lead_model)"
  LEAD_EFFORT="$(codex_lead_effort)"
  prepare_codex_runtime || exit $?

  case "$first" in
    /gsd-*) CODEX_COMMAND="\$${first#/}" ;;
    \$gsd-*) CODEX_COMMAND="$first" ;;
    *) echo "gsd-run: unsupported Codex GSD command: $first" >&2; exit 2 ;;
  esac
  [ "$#" -eq 0 ] || CODEX_COMMAND="$CODEX_COMMAND $*"
  # Codex exposes two lifetimes: the orchestration cell and the long-lived
  # child PTY. Make the distinction explicit to the autonomous executor so a
  # yielded cell cannot be mistaken for a failed stateful command and retried.
  [ -z "$CODEX_SESSION_CONTRACT" ] || CODEX_COMMAND="$CODEX_COMMAND

$CODEX_SESSION_CONTRACT"
  RUN=(env CODEX_HOME="$CODEX_RUNTIME_HOME" "$CODEX_BIN" exec
    -c "model=\"$LEAD_MODEL\""
    -c "model_reasoning_effort=\"$LEAD_EFFORT\""
    --dangerously-bypass-approvals-and-sandbox
    "$CODEX_COMMAND")
else
  CLAUDE_BIN="${CLAUDE_BIN:-claude}"
  LEAD_MODEL="$(claude_lead_model)"
  case "$first" in
    \$gsd-*) first="/${first#\$}" ;;
    /gsd-*) ;;
    *) echo "gsd-run: unsupported Claude GSD command: $first" >&2; exit 2 ;;
  esac
  CMD_STR="$first"
  [ "$#" -eq 0 ] || CMD_STR="$CMD_STR $*"
  CLAUDE_ARGS=(--strict-mcp-config --mcp-config '{"mcpServers":{}}'
    --dangerously-skip-permissions --model "$LEAD_MODEL" -p "$CMD_STR")
  RUN=(env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN
    "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}")
fi

# STATEFUL BOUNDARY: exactly one drive is launched. Its output is never mined
# for availability words and no failure/timeout is replayed on another vendor.
run_bounded "$TIMEOUT_SECS" "${RUN[@]}" </dev/null 2>&1 | tee "$LOG_FILE"
rc="${PIPESTATUS[0]}"
if [ "$rc" -ne 0 ]; then
  echo "gsd-run: stateful drive failed on $SELECTED_HOST (rc=$rc); cross-vendor replay is forbidden" >&2
  echo "gsd-run: fix availability if needed, then resume on $SELECTED_HOST from .planning state; log: $LOG_FILE" >&2
fi
exit "$rc"
