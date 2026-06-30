#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ruflo-host-executor.sh --prompt-file FILE [--cwd DIR] [--host codex|claude] [--model-tier TIER] [--full-access]

Executes a Ruflo-tracked task through the active host CLI instead of a provider API key.
Ruflo coordinates swarms, roles, memory, and routing; Codex/Claude CLI performs the work
through the user's existing OAuth/session.

--cwd must resolve under the repo root (or HOST_EXECUTOR_ROOT); '..' escapes are rejected.
--full-access (or HOST_EXECUTOR_FULL_ACCESS=1) opts the Codex host into danger-full-access;
the default is the least-permissive workspace-write sandbox.
EOF
}

PROMPT_FILE=""
RUN_CWD="${PWD}"
HOST="${RUFLO_HOST:-}"
MODEL_TIER="${RUFLO_MODEL_TIER:-sonnet}"
# codex-gate spec-205-gate-hardening (FINDING 4): default to the least-permissive
# Codex sandbox; danger-full-access requires explicit opt-in.
FULL_ACCESS="${HOST_EXECUTOR_FULL_ACCESS:-0}"

# codex-gate spec-205-gate-hardening (FINDING 4): containment root the RUN_CWD must
# resolve under. Override with HOST_EXECUTOR_ROOT (e.g. an approved worktree);
# defaults to the repo root containing this script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt-file) PROMPT_FILE="$2"; shift 2 ;;
    --cwd) RUN_CWD="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --model-tier|--model) MODEL_TIER="$2"; shift 2 ;;
    # codex-gate spec-205-gate-hardening (FINDING 4): explicit opt-in for danger-full-access.
    --full-access) FULL_ACCESS="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$PROMPT_FILE" || ! -f "$PROMPT_FILE" ]]; then
  echo "ERROR: --prompt-file is required and must exist" >&2
  exit 2
fi

if [[ ! -d "$RUN_CWD" ]]; then
  echo "ERROR: --cwd does not exist: $RUN_CWD" >&2
  exit 2
fi

# codex-gate spec-205-gate-hardening (FINDING 4): constrain RUN_CWD to the repo root
# (or an approved HOST_EXECUTOR_ROOT). Reject any cwd that resolves outside the root so
# an arbitrary prompt task cannot run the host CLI at an uncontrolled filesystem location.
CONTAINMENT_ROOT="${HOST_EXECUTOR_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd -P)}"
CONTAINMENT_ROOT="$(cd "$CONTAINMENT_ROOT" 2>/dev/null && pwd -P)" || {
  echo "ERROR: containment root does not exist: ${HOST_EXECUTOR_ROOT:-$SCRIPT_DIR/../..}" >&2
  exit 2
}
RUN_CWD_RESOLVED="$(cd "$RUN_CWD" && pwd -P)"
if [[ "$RUN_CWD_RESOLVED" != "$CONTAINMENT_ROOT" && "$RUN_CWD_RESOLVED" != "$CONTAINMENT_ROOT"/* ]]; then
  echo "ERROR: --cwd escapes containment root: $RUN_CWD_RESOLVED not under $CONTAINMENT_ROOT" >&2
  exit 2
fi
RUN_CWD="$RUN_CWD_RESOLVED"

detect_host() {
  if [[ -n "$HOST" ]]; then
    echo "$HOST"
    return
  fi
  if [[ -n "${CODEX_SESSION_ID:-}" || -n "${CODEX_THREAD_ID:-}" || -n "${CODEX_HOME:-}" || -n "${CODEX_BIN:-}" ]]; then
    echo "codex"
    return
  fi
  if [[ -n "${CLAUDECODE:-}" || -n "${CLAUDE_AGENT_DEPTH:-}" || -n "${CLAUDECODE_SESSION_ID:-}" ]]; then
    echo "claude"
    return
  fi
  if command -v codex >/dev/null 2>&1; then
    echo "codex"
    return
  fi
  if command -v claude >/dev/null 2>&1; then
    echo "claude"
    return
  fi
  echo "ERROR: no supported host CLI found; install/login to codex or claude" >&2
  exit 127
}

map_model() {
  local host="$1"
  local tier="$2"

  case "$host:$tier" in
    codex:haiku|codex:simple|codex:low|codex:gpt-5.3-codex-spark) echo "gpt-5.3-codex-spark" ;;
    codex:sonnet|codex:standard|codex:med|codex:gpt-5.4) echo "gpt-5.4" ;;
    codex:opus|codex:extended|codex:high|codex:max|codex:gpt-5.5) echo "gpt-5.5" ;;
    claude:gpt-5.3-codex-spark|claude:haiku|claude:simple|claude:low) echo "haiku" ;;
    claude:gpt-5.4|claude:sonnet|claude:standard|claude:med) echo "sonnet" ;;
    claude:gpt-5.5|claude:opus|claude:extended|claude:high|claude:max) echo "opus" ;;
    *) echo "$tier" ;;
  esac
}

HOST="$(detect_host)"
case "$HOST" in
  codex|claude) ;;
  *) echo "ERROR: unsupported host: $HOST" >&2; exit 2 ;;
esac

MODEL="$(map_model "$HOST" "$MODEL_TIER")"

echo "[ruflo-host-executor] host=$HOST model=$MODEL cwd=$RUN_CWD prompt=$PROMPT_FILE" >&2

case "$HOST" in
  codex)
    command -v codex >/dev/null 2>&1 || { echo "ERROR: codex CLI not found" >&2; exit 127; }
    # codex-gate spec-205-gate-hardening (FINDING 4): least-privilege by default;
    # danger-full-access only on explicit opt-in, with an audit line recording the
    # approved cwd + prompt source.
    SANDBOX="workspace-write"
    if [[ "$FULL_ACCESS" == "1" ]]; then
      SANDBOX="danger-full-access"
      echo "[ruflo-host-executor][AUDIT] full-access approved sandbox=danger-full-access cwd=$RUN_CWD prompt-source=$PROMPT_FILE" >&2
    fi
    # Strip provider keys so Codex uses the logged-in OAuth/session path.
    env -u CODEX_API_KEY -u OPENAI_API_KEY -u OPENROUTER_API_KEY -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
      codex exec -C "$RUN_CWD" --sandbox "$SANDBOX" -m "$MODEL" - < "$PROMPT_FILE"
    ;;
  claude)
    command -v claude >/dev/null 2>&1 || { echo "ERROR: claude CLI not found" >&2; exit 127; }
    # Unset provider keys so Claude Code uses the logged-in subscription/OAuth session.
    # codex-gate: D1 - Claude must execute from the requested run cwd.
    (
      cd "$RUN_CWD"
      env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
        claude -p --model "$MODEL" --permission-mode auto
    ) < "$PROMPT_FILE"
    ;;
esac
