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
if ! [[ "$GSD_SKILL_NAME" =~ ^gsd-[a-z0-9][a-z0-9-]*$ ]]; then
  echo "gsd-run: invalid GSD command name (basename only): $GSD_SKILL_NAME" >&2
  exit 2
fi

GIT_BIN_FIXED=/usr/bin/git
[ -x "$GIT_BIN_FIXED" ] || { echo "gsd-run: trusted Git binary is unavailable at $GIT_BIN_FIXED" >&2; exit 78; }
REPO_ROOT="$($GIT_BIN_FIXED rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || exit 1

REAL_USER_HOME="$(/usr/bin/python3 - <<'PY'
import os, pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
)" || exit 1
CODEX_SOURCE_ROOT_FIXED="$REAL_USER_HOME/.codex"
USER_AGENTS_ROOT_FIXED="$REAL_USER_HOME/.agents"
GSD_PACKAGE_ROOT_FIXED="$SCRIPT_DIR/../../node_modules/@opengsd/gsd-core"
GSD_PACKAGE_FALLBACK_FIXED="$SCRIPT_DIR/../../packages/feature-fix-swarm/node_modules/@opengsd/gsd-core"
DANGER_GRANT_STORE_FIXED="$REAL_USER_HOME/.cache/feature-fix-swarm/danger-grants.json"
AUTH_LOCK_DIR_FIXED="$REAL_USER_HOME/.cache/feature-fix-swarm/codex-auth.lock"
FFS_USER_MANIFEST_FIXED="$REAL_USER_HOME/.cache/feature-fix-swarm/install-manifest.json"
AUTH_LOCK_ATTEMPTS_FIXED=100
TRUSTED_NODE_BIN_FIXED=""

# A linked worktree has its own git-dir but shares one common directory with
# the primary checkout. Runner ownership and resume state live under that
# common directory so two worktrees cannot start the same stateful drive.
GIT_COMMON_DIR=""
if _git_common="$($GIT_BIN_FIXED rev-parse --git-common-dir 2>/dev/null)"; then
  case "$_git_common" in
    /*) GIT_COMMON_DIR="$(cd "$_git_common" 2>/dev/null && pwd -P)" ;;
    *) GIT_COMMON_DIR="$(cd "$REPO_ROOT/$_git_common" 2>/dev/null && pwd -P)" ;;
  esac
fi
if [ -n "$GIT_COMMON_DIR" ]; then
  PROJECT_PRIMARY_ROOT="$(dirname "$GIT_COMMON_DIR")"
  DEFAULT_RUN_STATE_DIR="$GIT_COMMON_DIR/ffs/gsd-run"
else
  PROJECT_PRIMARY_ROOT="$REPO_ROOT"
  DEFAULT_RUN_STATE_DIR="$REPO_ROOT/.planning/run-state"
fi
unset _git_common

# gsd's mempalace commands call a bare `mempalace` binary in headless mode.
export PATH="$REPO_ROOT/scripts/gsd:$PATH"

# execute-plan marks every PLAN frontmatter requirement complete without
# checking whether another plan still owns work for that ID. Guard the whole
# phase before even probing a model so an unsafe plan cannot mutate state.
if [ "$GSD_SKILL_NAME" = "gsd-execute-phase" ]; then
  if [ "$#" -lt 2 ]; then
    echo "gsd-run: gsd-execute-phase requires a phase number" >&2
    exit 2
  fi
  OWNERSHIP_GATE="$SCRIPT_DIR/requirement-ownership-gate.sh"
  if [ ! -f "$OWNERSHIP_GATE" ]; then
    echo "gsd-run: requirement ownership gate missing: $OWNERSHIP_GATE" >&2
    exit 78
  fi
  bash "$OWNERSHIP_GATE" "$2" || exit $?
  # Advisory scope-drift re-anchor (once per phase start, never per turn):
  # deterministic diff-vs-declared-surface + PHASE GOAL line. Fail-soft.
  DRIFT_GATE="$SCRIPT_DIR/scope-drift-gate.sh"
  if [ -f "$DRIFT_GATE" ]; then
    DRIFT_PLANS=()
    for _p in "$REPO_ROOT"/.planning/phases/*/*-PLAN.md; do
      [ -f "$_p" ] && DRIFT_PLANS+=(--plan "$_p")
    done
    if [ "${#DRIFT_PLANS[@]}" -gt 0 ]; then
      bash "$DRIFT_GATE" "${DRIFT_PLANS[@]}" || true
    fi
  fi
fi

TIMEOUT_SECS="${TIMEOUT:-900}"
PROBE_TIMEOUT_SECS="${GSD_HOST_PROBE_TIMEOUT:-45}"
PROBE_MARKER="FFS_HOST_PROBE_READY"
PROBE_PROMPT="This is a read-only availability probe. Use no tools. Output exactly: $PROBE_MARKER"
LOG_DIR="$REPO_ROOT/.planning/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/gsd-run-${TS}.log"
RUN_STATE_DIR="${GSD_RUN_STATE_DIR:-$DEFAULT_RUN_STATE_DIR}"
RUN_PID_FILE="$RUN_STATE_DIR/gsd-run.pid"
RUN_STATUS_FILE="$RUN_STATE_DIR/gsd-run.status"
RUN_HEARTBEAT_FILE="$RUN_STATE_DIR/gsd-run.heartbeat"
RUN_RECLAIM_DIR="$RUN_STATE_DIR/gsd-run.reclaim"
RUN_TUPLE_FILE="$RUN_STATE_DIR/gsd-run.tuple"
RUN_HEARTBEAT_PID=""
RUN_STATE_OWNED=0
RUN_MACHINE_ID="${GSD_MACHINE_ID:-$(hostname 2>/dev/null || uname -n 2>/dev/null || printf unknown)}"
RUN_MACHINE_ID="$(printf '%s' "$RUN_MACHINE_ID" | LC_ALL=C tr -c 'A-Za-z0-9_.:-' '_' | cut -c1-128)"
[ -n "$RUN_MACHINE_ID" ] || RUN_MACHINE_ID=unknown
ACTIVE_HOST=""
MODEL_REQUEST_KIND=""
MODEL_REQUEST_NAME=""
MODEL_REQUEST_ID=""
EXACT_MODEL_REQUEST=0
EXACT_FABLE_REQUEST=0
MODEL_REQUEST_JSON="${GSD_MODEL_REQUEST:-}"
if [ -z "$MODEL_REQUEST_JSON" ]; then
  case "${GSD_LEAD_MODEL:-sonnet}" in
    sonnet) MODEL_REQUEST_JSON='{"kind":"tier","name":"execution"}' ;;
    opus) MODEL_REQUEST_JSON='{"kind":"tier","name":"judgment"}' ;;
    haiku) MODEL_REQUEST_JSON='{"kind":"tier","name":"volume"}' ;;
    fable) MODEL_REQUEST_JSON='{"kind":"exact","id":"claude-fable-5"}' ;;
    gpt-*|o[1-9]*|claude-*|gemini-*|deepseek-*|qwen-*|minimax-*)
      echo "gsd-run: raw vendor model ids require GSD_MODEL_REQUEST={kind:exact,id:...}" >&2
      exit 2
      ;;
    *) echo "gsd-run: unsupported legacy model alias: ${GSD_LEAD_MODEL}" >&2; exit 2 ;;
  esac
fi
MODEL_REQUEST_HELPER="$SCRIPT_DIR/../../lib/model_requests.py"
if [ ! -f "$MODEL_REQUEST_HELPER" ]; then
  MODEL_REQUEST_HELPER="$SCRIPT_DIR/../../packages/feature-fix-swarm/lib/model_requests.py"
fi
[ -f "$MODEL_REQUEST_HELPER" ] || { echo "gsd-run: typed model request helper is missing" >&2; exit 78; }
_model_resolution="$(/usr/bin/python3 "$MODEL_REQUEST_HELPER" resolve "$MODEL_REQUEST_JSON")" || exit $?
IFS='|' read -r MODEL_REQUEST_KIND MODEL_REQUEST_NAME MODEL_REQUEST_ID LEAD_TIER REQUESTED_MODEL REQUESTED_MODEL_EFFORT <<EOF
$(/usr/bin/python3 - "$_model_resolution" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
kind = d["kind"]
name = d.get("name", "")
model = d["model"]
effort = d.get("effort", "medium")
tier = {"judgment": "opus", "execution": "sonnet", "volume": "haiku"}.get(name, model)
print("|".join((kind, name, model if kind == "exact" else "", tier, model, effort)))
PY
)
EOF
unset _model_resolution
case "$REQUESTED_MODEL" in
  ""|*[!A-Za-z0-9._:/-]*) echo "gsd-run: model id contains unsupported characters" >&2; exit 2 ;;
esac
if [ "$MODEL_REQUEST_KIND" = exact ]; then
  EXACT_MODEL_REQUEST=1
  case "$MODEL_REQUEST_ID" in *fable*) EXACT_FABLE_REQUEST=1 ;; esac
fi
NETWORK_MODE="${GSD_NETWORK_MODE:-none}"
case "$NETWORK_MODE" in
  none|enabled) ;;
  *) echo "gsd-run: network_mode must be none or enabled (got: $NETWORK_MODE)" >&2; exit 2 ;;
esac
NETWORK_PURPOSE="${GSD_NETWORK_PURPOSE:-}"
case "$NETWORK_PURPOSE" in
  ""|docs|package-registry|general) ;;
  *) echo "gsd-run: network_purpose must be docs, package-registry, general, or empty" >&2; exit 2 ;;
esac
if [ "$NETWORK_MODE" = enabled ] && [ -z "$NETWORK_PURPOSE" ]; then
  echo "gsd-run: network_mode=enabled requires network_purpose=docs|package-registry|general for audit" >&2
  exit 2
fi
REQUESTED_SANDBOX_MODE="${GSD_SANDBOX_MODE:-workspace-write}"
case "$REQUESTED_SANDBOX_MODE" in
  workspace-write) ;;
  danger-full-access)
    if [ -z "${GSD_RUN_ID:-}" ]; then
      echo "gsd-run: danger-full-access requires an explicit GSD_RUN_ID bound to its grant" >&2
      exit 78
    fi
    if [ "$NETWORK_MODE" != enabled ]; then
      echo "gsd-run: danger-full-access requires network_mode=enabled because network denial is unenforceable unsandboxed" >&2
      exit 78
    fi
    if [ "$EXACT_FABLE_REQUEST" -eq 1 ]; then
      echo "gsd-run: exact Fable and Codex danger-full-access are incompatible" >&2
      exit 78
    fi
    ;;
  *) echo "gsd-run: sandbox mode must be workspace-write or danger-full-access" >&2; exit 2 ;;
esac
RESUME_REQUESTED="${GSD_RESUME:-0}"
if [ "$GSD_SKILL_NAME" = gsd-resume-work ]; then
  RESUME_REQUESTED=1
elif [ -f "$RUN_TUPLE_FILE" ] && [ -f "$RUN_STATUS_FILE" ] \
  && grep -q '^state=failed$' "$RUN_STATUS_FILE" \
  && grep -q "^skill=$GSD_SKILL_NAME$" "$RUN_STATUS_FILE"; then
  RESUME_REQUESTED=1
fi
RUN_ID="${GSD_RUN_ID:-}"
if [ -z "$RUN_ID" ] && [ "$RESUME_REQUESTED" = 1 ] && [ -f "$RUN_TUPLE_FILE" ]; then
  RUN_ID="$(sed -n 's/^run_id=//p' "$RUN_TUPLE_FILE" | head -1)"
fi
[ -n "$RUN_ID" ] || RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
_safe_run_id="$(printf '%s' "$RUN_ID" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '_' | cut -c1-128)"
if [ -n "${GSD_RUN_ID:-}" ] && [ "$_safe_run_id" != "$RUN_ID" ]; then
  echo "gsd-run: explicit GSD_RUN_ID contains unsupported characters or exceeds 128 bytes" >&2
  exit 2
fi
RUN_ID="$_safe_run_id"
unset _safe_run_id
RUN_WORKTREE_ROOT="$PROJECT_PRIMARY_ROOT/.claude/worktrees/$RUN_ID"
CODEX_RUNTIME_HOME=""
CODEX_CLI_VERSION=""
CODEX_PREFLIGHT_FATAL=0
CODEX_AUTH_SOURCE=""
CODEX_AUTH_INITIAL_HASH=""
SKILL_HASH=""
ROLE_CONFIG_HASH=""
BUNDLE_HASH=""
FFS_SKILL_HASH=""
SANDBOX_GRANT_CONSUMPTION="none"
ADVERSARY_DEGRADED=false
SELECTED_CODEX_MODEL=""
SELECTED_CODEX_EFFORT=""
SELECTED_CLAUDE_MODEL=""

write_run_status() {
  local state="$1" exit_code="${2:-}" tmp
  [ "$RUN_STATE_OWNED" -eq 1 ] || return 0
  tmp="$(mktemp "$RUN_STATE_DIR/.gsd-run.status.XXXXXX")" || return 1
  {
    printf 'state=%s\n' "$state"
    printf 'pid=%s\n' "$$"
    printf 'machine=%s\n' "$RUN_MACHINE_ID"
    printf 'host=%s\n' "${SELECTED_HOST:-${ACTIVE_HOST:-unknown}}"
    printf 'skill=%s\n' "$GSD_SKILL_NAME"
    printf 'log=%s\n' "$LOG_FILE"
    [ -z "$exit_code" ] || printf 'exit_code=%s\n' "$exit_code"
    printf 'updated_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$tmp"
  atomic_replace "$tmp" "$RUN_STATUS_FILE"
}

atomic_replace() {
  python3 -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$1" "$2"
}

write_heartbeat() {
  local tmp
  tmp="$(mktemp "$RUN_STATE_DIR/.gsd-run.heartbeat.XXXXXX")" || return 1
  if ! atomic_replace "$tmp" "$RUN_HEARTBEAT_FILE"; then
    rm -f "$tmp"
    return 1
  fi
}

file_epoch() {
  local value
  value="$(stat -f %m "$1" 2>/dev/null || true)"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    value="$(stat -c %Y "$1" 2>/dev/null || true)"
  fi
  [[ "$value" =~ ^[0-9]+$ ]] && printf '%s\n' "$value"
}

claim_pidfile() {
  local claimed_epoch
  claimed_epoch="$(date +%s)"
  (
    set -C
    {
      printf '%s\n' "$$"
      printf 'machine=%s\n' "$RUN_MACHINE_ID"
      printf 'claimed_epoch=%s\n' "$claimed_epoch"
    } > "$RUN_PID_FILE"
  ) 2>/dev/null
}

foreign_lease_epoch() {
  local claimed="$1" heartbeat best=0
  heartbeat="$(file_epoch "$RUN_HEARTBEAT_FILE" 2>/dev/null || true)"
  [[ "$claimed" =~ ^[0-9]+$ ]] && best="$claimed"
  if [[ "$heartbeat" =~ ^[0-9]+$ ]] && [ "$heartbeat" -gt "$best" ]; then
    best="$heartbeat"
  fi
  [ "$best" -gt 0 ] && printf '%s\n' "$best"
}

release_reclaim_mutex() {
  rm -f "$RUN_RECLAIM_DIR/owner"
  rmdir "$RUN_RECLAIM_DIR" 2>/dev/null || true
}

acquire_run_state() {
  local live_pid="" owner_machine="" claimed_epoch="" heartbeat_epoch=""
  local heartbeat_secs lease_secs reclaim_lease_secs reclaim_epoch now age
  local attempt=0 owns_reclaim=0 state_path
  [ ! -L "$RUN_STATE_DIR" ] || {
    echo "gsd-run: refusing symlinked run-state directory: $RUN_STATE_DIR" >&2
    return 75
  }
  mkdir -p "$RUN_STATE_DIR"
  for state_path in "$RUN_PID_FILE" "$RUN_STATUS_FILE" "$RUN_HEARTBEAT_FILE" "$RUN_RECLAIM_DIR"; do
    [ ! -L "$state_path" ] || {
      echo "gsd-run: refusing symlinked run-state path: $state_path" >&2
      return 75
    }
  done

  lease_secs="${GSD_FOREIGN_LEASE_SECS:-120}"
  case "$lease_secs" in ''|*[!0-9]*|0) lease_secs=120 ;; esac
  reclaim_lease_secs="${GSD_RECLAIM_LEASE_SECS:-30}"
  case "$reclaim_lease_secs" in ''|*[!0-9]*|0) reclaim_lease_secs=30 ;; esac

  # The pidfile is the ownership primitive: noclobber performs an atomic
  # exclusive create, so contenders can never observe an empty owner lock.
  # A short-lived reclaim mutex only serializes stale-owner removal.
  while [ "$attempt" -lt 20 ]; do
    attempt=$((attempt + 1))
    if [ -d "$RUN_RECLAIM_DIR" ]; then
      reclaim_epoch="$(file_epoch "$RUN_RECLAIM_DIR" 2>/dev/null || true)"
      now="$(date +%s)"
      if [[ "$reclaim_epoch" =~ ^[0-9]+$ ]] \
         && [ $((now - reclaim_epoch)) -gt "$reclaim_lease_secs" ]; then
        release_reclaim_mutex
        continue
      fi
      sleep 0.05
      continue
    fi
    if claim_pidfile; then
      RUN_STATE_OWNED=1
      break
    fi
    [ ! -L "$RUN_PID_FILE" ] || {
      echo "gsd-run: refusing symlinked run-state path: $RUN_PID_FILE" >&2
      return 75
    }
    live_pid="$(head -1 "$RUN_PID_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
    owner_machine="$(sed -n 's/^machine=//p' "$RUN_PID_FILE" 2>/dev/null | head -1)"
    claimed_epoch="$(sed -n 's/^claimed_epoch=//p' "$RUN_PID_FILE" 2>/dev/null | head -1)"
    if { [ -z "$owner_machine" ] || [ "$owner_machine" = "$RUN_MACHINE_ID" ]; } \
       && [[ "$live_pid" =~ ^[0-9]+$ ]] && kill -0 "$live_pid" 2>/dev/null; then
      echo "gsd-run: active drive already owns $RUN_PID_FILE (pid=$live_pid machine=$owner_machine); refusing duplicate launch" >&2
      return 75
    fi
    if [ -n "$owner_machine" ] && [ "$owner_machine" != "$RUN_MACHINE_ID" ]; then
      heartbeat_epoch="$(foreign_lease_epoch "$claimed_epoch" 2>/dev/null || true)"
      now="$(date +%s)"
      if [[ "$heartbeat_epoch" =~ ^[0-9]+$ ]]; then
        age=$((now - heartbeat_epoch))
        if [ "$age" -le "$lease_secs" ]; then
          echo "gsd-run: foreign owner holds a fresh lease on $RUN_PID_FILE (pid=$live_pid machine=$owner_machine age=${age}s); refusing duplicate launch" >&2
          return 75
        fi
      fi
    fi

    if ! mkdir "$RUN_RECLAIM_DIR" 2>/dev/null; then
      sleep 0.05
      continue
    fi
    owns_reclaim=1
    printf '%s\nmachine=%s\n' "$$" "$RUN_MACHINE_ID" > "$RUN_RECLAIM_DIR/owner"

    # Re-read under the reclaim mutex. Never remove an owner that became
    # live or refreshed its foreign-machine lease after the first read.
    live_pid="$(head -1 "$RUN_PID_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
    owner_machine="$(sed -n 's/^machine=//p' "$RUN_PID_FILE" 2>/dev/null | head -1)"
    claimed_epoch="$(sed -n 's/^claimed_epoch=//p' "$RUN_PID_FILE" 2>/dev/null | head -1)"
    if { [ -z "$owner_machine" ] || [ "$owner_machine" = "$RUN_MACHINE_ID" ]; } \
       && [[ "$live_pid" =~ ^[0-9]+$ ]] && kill -0 "$live_pid" 2>/dev/null; then
      release_reclaim_mutex
      echo "gsd-run: active drive already owns $RUN_PID_FILE (pid=$live_pid machine=$owner_machine); refusing duplicate launch" >&2
      return 75
    fi
    if [ -n "$owner_machine" ] && [ "$owner_machine" != "$RUN_MACHINE_ID" ]; then
      heartbeat_epoch="$(foreign_lease_epoch "$claimed_epoch" 2>/dev/null || true)"
      now="$(date +%s)"
      if [[ "$heartbeat_epoch" =~ ^[0-9]+$ ]] && [ $((now - heartbeat_epoch)) -le "$lease_secs" ]; then
        release_reclaim_mutex
        echo "gsd-run: foreign owner holds a fresh lease on $RUN_PID_FILE (pid=$live_pid machine=$owner_machine); refusing duplicate launch" >&2
        return 75
      fi
    fi
    rm -f "$RUN_PID_FILE"
    if claim_pidfile; then
      RUN_STATE_OWNED=1
      release_reclaim_mutex
      owns_reclaim=0
      break
    fi
    release_reclaim_mutex
    owns_reclaim=0
  done

  [ "$RUN_STATE_OWNED" -eq 1 ] || {
    [ "$owns_reclaim" -eq 0 ] || release_reclaim_mutex
    echo "gsd-run: run-state ownership remained contended; refusing duplicate launch" >&2
    return 75
  }

  write_heartbeat || return 1
  write_run_status probing
  heartbeat_secs="${GSD_HEARTBEAT_SECS:-15}"
  case "$heartbeat_secs" in ''|*[!0-9]*|0) heartbeat_secs=15 ;; esac
  (
    heartbeat_sleep=""
    trap '[ -z "$heartbeat_sleep" ] || kill "$heartbeat_sleep" 2>/dev/null || true; exit 0' TERM INT
    while kill -0 "$$" 2>/dev/null; do
      if ! write_heartbeat; then
        echo "gsd-run: heartbeat refresh failed; terminating drive rather than losing its lease" >&2
        kill -TERM "$$" 2>/dev/null || true
        exit 1
      fi
      sleep "$heartbeat_secs" &
      heartbeat_sleep=$!
      wait "$heartbeat_sleep" 2>/dev/null || true
      heartbeat_sleep=""
    done
  ) &
  RUN_HEARTBEAT_PID=$!
  echo "gsd-run: liveness pidfile=$RUN_PID_FILE pid=$$ machine=$RUN_MACHINE_ID status=$RUN_STATUS_FILE log=$LOG_FILE" >&2
}

ensure_run_worktree() {
  local parent expected_parent actual_common
  [ -n "$GIT_COMMON_DIR" ] || {
    echo "gsd-run: a real Git repository is required to create the run worktree" >&2
    return 78
  }
  parent="$PROJECT_PRIMARY_ROOT/.claude/worktrees"
  mkdir -p "$parent" || return 1
  [ ! -L "$parent" ] || {
    echo "gsd-run: refusing symlinked worktree parent: $parent" >&2
    return 78
  }
  expected_parent="$(cd "$parent" && pwd -P)" || return 1
  RUN_WORKTREE_ROOT="$expected_parent/$RUN_ID"
  if [ -e "$RUN_WORKTREE_ROOT" ]; then
    [ ! -L "$RUN_WORKTREE_ROOT" ] || {
      echo "gsd-run: refusing symlinked run worktree: $RUN_WORKTREE_ROOT" >&2
      return 78
    }
    actual_common="$($GIT_BIN_FIXED -C "$RUN_WORKTREE_ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
    [ -n "$actual_common" ] || {
      echo "gsd-run: existing run path is not a registered Git worktree: $RUN_WORKTREE_ROOT" >&2
      return 78
    }
    case "$actual_common" in
      /*) actual_common="$(cd "$actual_common" && pwd -P)" ;;
      *) actual_common="$(cd "$RUN_WORKTREE_ROOT/$actual_common" && pwd -P)" ;;
    esac
    if [ "$actual_common" != "$GIT_COMMON_DIR" ]; then
      echo "gsd-run: run worktree belongs to a different Git common directory" >&2
      return 78
    fi
    return 0
  fi
  "$GIT_BIN_FIXED" -C "$REPO_ROOT" worktree add --detach "$RUN_WORKTREE_ROOT" HEAD >/dev/null || {
    echo "gsd-run: failed to create run worktree at $RUN_WORKTREE_ROOT" >&2
    return 1
  }
  # GSD planning state is commonly untracked. Seed it once into the isolated
  # worktree, then leave the run-local copy untouched so resume is deterministic.
  if [ -d "$REPO_ROOT/.planning" ] && [ ! -L "$REPO_ROOT/.planning" ] \
     && [ ! -e "$RUN_WORKTREE_ROOT/.planning" ]; then
    cp -R "$REPO_ROOT/.planning" "$RUN_WORKTREE_ROOT/.planning" || return 1
  fi
}

CODEX_SESSION_CONTRACT="${GSD_CODEX_SESSION_CONTRACT:-FFS CODEX EXEC-SESSION CONTRACT: A tool result saying 'Script running with cell ID' is not completion or failure. Wait on that yielded cell. If the wait result contains a session_id and no exit_code, the nested process is still alive: poll that exact session with write_stdin until it exits. If the tool session is lost, check runner liveness with kill -0 \$(head -1 \"$RUN_PID_FILE\"); never launch a replacement while that pid is alive. Any worktree created for this run must live under \"$RUN_WORKTREE_ROOT\".}"

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"
  fi
}

sha256_tree() {
  python3 - "$@" <<'PY'
import hashlib, os, pathlib, sys
digest = hashlib.sha256()
for raw_root in sys.argv[1:]:
    root = pathlib.Path(raw_root)
    if not root.exists():
        continue
    files = [root] if root.is_file() else [item for item in root.rglob("*") if item.is_file()]
    for path in sorted(files, key=lambda p: p.name if root.is_file() else str(p.relative_to(root))):
        if path.is_symlink():
            raise SystemExit(f"refusing symlink inside hashed runtime tree: {path}")
        relative = (path.name if root.is_file() else path.relative_to(root).as_posix()).encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
print(digest.hexdigest())
PY
}

compute_ffs_skill_hash() {
  local source_skills
  source_skills="$SCRIPT_DIR/../../skills"
  if [ -d "$SCRIPT_DIR/../../packages/feature-fix-swarm/skills" ]; then
    source_skills="$SCRIPT_DIR/../../packages/feature-fix-swarm/skills"
  fi
  python3 "$SCRIPT_DIR/hash-ffs-skills.py" \
    "$REPO_ROOT" "$source_skills" \
    "$REPO_ROOT/.feature-fix-swarm/install-manifest.json" "$FFS_USER_MANIFEST_FIXED"
}

sync_codex_auth() {
  local runtime_auth refreshed_hash output rc
  [ -n "$CODEX_RUNTIME_HOME" ] || return 0
  runtime_auth="$CODEX_RUNTIME_HOME/auth.json"
  [ -f "$runtime_auth" ] || return 0
  [ -n "$CODEX_AUTH_SOURCE" ] || return 0
  refreshed_hash="$(sha256_file "$runtime_auth")" || return 1
  [ "$refreshed_hash" != "$CODEX_AUTH_INITIAL_HASH" ] || return 0

  output="$(/usr/bin/python3 "$SCRIPT_DIR/sync-codex-auth.py" \
    "$CODEX_AUTH_SOURCE" "$runtime_auth" "$CODEX_AUTH_INITIAL_HASH" \
    "$AUTH_LOCK_DIR_FIXED" --attempts "$AUTH_LOCK_ATTEMPTS_FIXED")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "gsd-run: OAuth refresh was not synchronized (rc=$rc)" >&2
    return "$rc"
  fi
  if printf '%s\n' "$output" | grep -q 'concurrent-refresh-preserved'; then
    echo "gsd-run: OAuth refresh was not synchronized because auth.json changed concurrently" >&2
  fi
  return 0
}

cleanup_codex_runtime() {
  local rc=0
  if [ -n "$CODEX_RUNTIME_HOME" ] && [ -d "$CODEX_RUNTIME_HOME" ]; then
    sync_codex_auth || rc=$?
    rm -rf "$CODEX_RUNTIME_HOME"
  fi
  return "$rc"
}

cleanup_runner() {
  local rc="$?" auth_rc=0 recorded_pid="" recorded_machine=""
  trap - EXIT
  if [ -n "$RUN_HEARTBEAT_PID" ]; then
    kill "$RUN_HEARTBEAT_PID" 2>/dev/null || true
    wait "$RUN_HEARTBEAT_PID" 2>/dev/null || true
  fi
  cleanup_codex_runtime || auth_rc=$?
  if [ "$auth_rc" -ne 0 ]; then
    echo "gsd-run: OAuth refresh synchronization failed (rc=$auth_rc)" >&2
    [ "$rc" -ne 0 ] || rc="$auth_rc"
  fi
  if [ "$RUN_STATE_OWNED" -eq 1 ]; then
    write_run_status "$([ "$rc" -eq 0 ] && echo completed || echo failed)" "$rc"
    [ ! -f "$RUN_PID_FILE" ] || recorded_pid="$(head -1 "$RUN_PID_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
    [ ! -f "$RUN_PID_FILE" ] || recorded_machine="$(sed -n 's/^machine=//p' "$RUN_PID_FILE" 2>/dev/null | head -1)"
    if [ "$recorded_pid" = "$$" ] && [ "$recorded_machine" = "$RUN_MACHINE_ID" ]; then
      rm -f "$RUN_PID_FILE"
    fi
  fi
  exit "$rc"
}

trap cleanup_runner EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

acquire_run_state || exit $?
ACTIVE_HOST="$(detect_orchestrator_host)" || exit $?
if [ "$EXACT_MODEL_REQUEST" -eq 1 ]; then
  case "$MODEL_REQUEST_ID" in
    gpt-*|o[1-9]*) ACTIVE_HOST=codex ;;
    claude-*) ACTIVE_HOST=claude ;;
    *) echo "gsd-run: exact model vendor is unsupported by this runner: $MODEL_REQUEST_ID" >&2; exit 2 ;;
  esac
fi
if [ "$EXACT_FABLE_REQUEST" -eq 1 ]; then
  if [ "$NETWORK_MODE" = none ]; then
    echo "gsd-run: exact Fable requires network_mode=enabled; Claude network denial is not enforceable" >&2
    exit 78
  fi
  ACTIVE_HOST=claude
elif [ "$REQUESTED_SANDBOX_MODE" = danger-full-access ] || [ "$NETWORK_MODE" = none ]; then
  ACTIVE_HOST=codex
fi

host_label() {
  case "$1" in codex) echo Codex ;; claude) echo Claude ;; esac
}

alternate_host() {
  case "$1" in codex) echo claude ;; claude) echo codex ;; esac
}

codex_lead_model() {
  if [ "$EXACT_MODEL_REQUEST" -eq 1 ]; then
    printf '%s\n' "$REQUESTED_MODEL"
    return
  fi
  codex_equiv_model "$LEAD_TIER" 2>/dev/null || printf '%s\n' "$LEAD_TIER"
}

codex_lead_effort() {
  if [ "$EXACT_MODEL_REQUEST" -eq 1 ]; then
    printf '%s\n' "$REQUESTED_MODEL_EFFORT"
    return
  fi
  local effort
  effort="$(codex_equiv_effort "$LEAD_TIER" 2>/dev/null || true)"
  printf '%s\n' "${GSD_LEAD_EFFORT:-${effort:-high}}"
}

claude_lead_model() {
  if [ "$EXACT_MODEL_REQUEST" -eq 1 ]; then
    printf '%s\n' "$REQUESTED_MODEL"
    return
  fi
  local model
  model="$(claude_equiv_model "$LEAD_TIER" 2>/dev/null || true)"
  if [ -z "$model" ] || [ "$model" = "$LEAD_TIER" ]; then
    case "$LEAD_TIER" in
      sonnet|claude-sonnet-*) model="claude-sonnet-5" ;;
      opus|claude-opus-*) model="claude-opus-5" ;;
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
  sanitized="$(printf '%s' "$raw" | /usr/bin/python3 -c '
import re, sys
s = sys.stdin.read()
s = re.sub(r"https?://\S+", "[REDACTED_URL]", s, flags=re.I)
s = re.sub(r"(bearer\s+)\S+", r"\1[REDACTED]", s, flags=re.I)
s = re.sub(r"(\b[a-z_]*(?:token|secret|password|pass|key|auth|credential)[a-z_]*\b\s*[=:]\s*)(?:\"[^\"]*\"|[^\s,;}]+)", r"\1[REDACTED]", s, flags=re.I)
s = re.sub(r"\b[A-Za-z0-9_+/=-]{24,}\b", "[REDACTED]", s)
sys.stdout.write(s[:int(sys.argv[1])])
' "$limit")"
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
  printf '%s\n' "$CODEX_SOURCE_ROOT_FIXED"
}

codex_skill_root() {
  if [ -f "$REPO_ROOT/.agents/skills/$GSD_SKILL_NAME/SKILL.md" ]; then
    printf '%s\n' "$REPO_ROOT/.agents/skills"
  else
    printf '%s\n' "$USER_AGENTS_ROOT_FIXED/skills"
  fi
}

trusted_gsd_package_root() {
  local candidate
  candidate="$GSD_PACKAGE_ROOT_FIXED"
  if [ ! -f "$candidate/package.json" ]; then
    candidate="$GSD_PACKAGE_FALLBACK_FIXED"
  fi
  if [ ! -f "$candidate/package.json" ]; then
    echo "gsd-run: pinned @opengsd/gsd-core package unavailable for hook verification: $candidate" >&2
    return 78
  fi
  printf '%s\n' "$candidate"
}

trusted_node_bin() {
  local candidate real metadata owner mode
  for candidate in "$TRUSTED_NODE_BIN_FIXED" /opt/homebrew/bin/node /usr/local/bin/node /usr/bin/node; do
    [ -n "$candidate" ] || continue
    [ -x "$candidate" ] || continue
    real="$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$candidate")" || continue
    [ -x "$real" ] || continue
    metadata="$(/usr/bin/python3 - "$real" <<'PY'
import os, stat, sys
s = os.stat(sys.argv[1])
print(s.st_uid, stat.S_IMODE(s.st_mode))
PY
)" || continue
    read -r owner mode <<EOF
$metadata
EOF
    if { [ "$owner" -eq 0 ] || [ "$owner" -eq "$(/usr/bin/id -u)" ]; } && [ $((mode & 18)) -eq 0 ]; then
      printf '%s\n' "$real"
      return 0
    fi
  done
  echo "gsd-run: no trusted absolute Node binary found in the fixed search path" >&2
  return 78
}

version_in_supported_codex_range() {
  local version="$1" major minor patch
  IFS=. read -r major minor patch <<EOF
$version
EOF
  case "$major:$minor:$patch" in *[!0-9:]*|::*|*::) return 1 ;; esac
  [ "$major" -eq 0 ] && [ "$minor" -ge 137 ] && [ "$minor" -lt 147 ]
}

require_supported_codex_cli() {
  local bin="$1" raw version
  [ -n "$CODEX_CLI_VERSION" ] && return 0
  raw="$("$bin" --version 2>/dev/null)" || {
    echo "gsd-run: could not determine Codex CLI version" >&2
    return 78
  }
  version="$(printf '%s\n' "$raw" | sed -nE 's/.*[^0-9]([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | head -1)"
  if [ -z "$version" ] || ! version_in_supported_codex_range "$version"; then
    CODEX_PREFLIGHT_FATAL=1
    echo "gsd-run: Codex CLI ${version:-unknown} is outside supported range >=0.137.0,<0.147.0" >&2
    return 78
  fi
  CODEX_CLI_VERSION="$version"
}

reject_custom_codex_provider() {
  local root="$1" config
  config="$root/config.toml"
  if [ -n "${OPENAI_BASE_URL:-}" ] || [ -n "${OPENAI_API_BASE:-}" ] \
     || [ -n "${CODEX_MODEL_PROVIDER:-}" ]; then
    CODEX_PREFLIGHT_FATAL=1
    echo "gsd-run: custom model providers are unsupported for subscription-backed runs" >&2
    return 78
  fi
  if [ -f "$config" ] && grep -Eq '^[[:space:]]*(model_provider[[:space:]]*=|\[model_providers\.)' "$config"; then
    CODEX_PREFLIGHT_FATAL=1
    echo "gsd-run: custom model providers are unsupported in $config" >&2
    return 78
  fi
}

command_surface_available() {
  local kind="$1" root skill_root manifest
  if [ "$kind" = "codex" ]; then
    root="$(codex_source_root)"
    skill_root="$(codex_skill_root)"
    if [ ! -f "$skill_root/$GSD_SKILL_NAME/SKILL.md" ]; then
      echo "gsd-run: exact $GSD_SKILL_NAME surface unavailable for Codex at $skill_root/$GSD_SKILL_NAME/SKILL.md" >&2
      return 78
    fi
    if ! find "$root/agents" -maxdepth 1 -name 'gsd-*.toml' -print -quit \
         2>/dev/null | grep -q .; then
      echo "gsd-run: exact $GSD_SKILL_NAME surface unavailable for Codex: no gsd-*.toml agents under $root/agents" >&2
      return 78
    fi
    manifest="$root/gsd-file-manifest.json"
    if [ ! -f "$manifest" ]; then
      echo "gsd-run: verified GSD Codex manifest unavailable at $manifest" >&2
      return 78
    fi
    reject_custom_codex_provider "$root" || return $?
  else
    root="${GSD_CLAUDE_SKILLS_ROOT:-$HOME/.claude/skills}"
    if [ ! -f "$root/$GSD_SKILL_NAME/SKILL.md" ]; then
      echo "gsd-run: exact $GSD_SKILL_NAME surface unavailable for Claude at $root/$GSD_SKILL_NAME/SKILL.md" >&2
      return 78
    fi
  fi
}

prepare_codex_runtime() {
  local source_root skill_root source_skill auth_source network_bool writable_json root skill trusted_package node_bin auth_meta auth_uid auth_mode user_skill_root
  source_root="$(codex_source_root)"
  skill_root="$(codex_skill_root)"
  command_surface_available codex || return $?
  CODEX_RUNTIME_HOME="$(mktemp -d "${TMPDIR:-/tmp}/ffs-gsd-codex.XXXXXX")" || return 1
  mkdir -p "$CODEX_RUNTIME_HOME/skills"
  # Build one canonical merged GSD skill tree: user scope first, then project
  # overrides. Never consult the retired CODEX_HOME/skills surface.
  user_skill_root="$USER_AGENTS_ROOT_FIXED/skills"
  for root in "$user_skill_root" "$REPO_ROOT/.agents/skills"; do
    [ -d "$root" ] || continue
    for skill in "$root"/gsd-*; do
      [ -d "$skill" ] || continue
      rm -rf "$CODEX_RUNTIME_HOME/skills/$(basename "$skill")"
      cp -RL "$skill" "$CODEX_RUNTIME_HOME/skills/$(basename "$skill")" || return 1
    done
  done
  source_skill="$skill_root/$GSD_SKILL_NAME"
  rm -rf "$CODEX_RUNTIME_HOME/skills/$GSD_SKILL_NAME"
  cp -RL "$source_skill" "$CODEX_RUNTIME_HOME/skills/$GSD_SKILL_NAME" || return 1
  SKILL_HASH="$(sha256_tree "$CODEX_RUNTIME_HOME/skills")" || return 1
  trusted_package="$(trusted_gsd_package_root)" || return $?
  node_bin="$(trusted_node_bin)" || return $?
  /usr/bin/python3 "$SCRIPT_DIR/codex-runtime-bundle.py" \
    "$source_root" "$trusted_package" "$node_bin" "$CODEX_RUNTIME_HOME" "$RUN_WORKTREE_ROOT" || return $?

  network_bool=false
  [ "$NETWORK_MODE" = enabled ] && network_bool=true
  writable_json="$(python3 -c 'import json,sys; print(json.dumps([sys.argv[1]]))' "$RUN_WORKTREE_ROOT")" || return 1
  {
    printf 'approval_policy = "never"\n'
    printf 'sandbox_mode = "%s"\n' "$REQUESTED_SANDBOX_MODE"
    python3 "$SCRIPT_DIR/sanitize-codex-config.py" "$source_root/config.toml" || return $?
    printf '\n[sandbox_workspace_write]\n'
    printf 'network_access = %s\n' "$network_bool"
    printf 'writable_roots = %s\n' "$writable_json"
  } > "$CODEX_RUNTIME_HOME/config.toml"
  auth_source="$CODEX_SOURCE_ROOT_FIXED/auth.json"
  if [ -L "$auth_source" ] || [ ! -f "$auth_source" ]; then
    echo "gsd-run: first-party Codex auth must be a regular non-symlink file at $auth_source" >&2
    return 78
  fi
  auth_meta="$(/usr/bin/python3 - "$auth_source" <<'PY'
import os, stat, sys
s = os.stat(sys.argv[1], follow_symlinks=False)
print(s.st_uid, stat.S_IMODE(s.st_mode))
PY
)" || return 78
  read -r auth_uid auth_mode <<EOF
$auth_meta
EOF
  if [ "$auth_uid" -ne "$(/usr/bin/id -u)" ] || [ "$auth_mode" -ne 384 ]; then
    echo "gsd-run: first-party Codex auth must be owned by the current user with mode 0600: $auth_source" >&2
    return 78
  fi
  CODEX_AUTH_SOURCE="$auth_source"
  CODEX_AUTH_INITIAL_HASH="$(sha256_file "$auth_source")" || return 1
  cp "$auth_source" "$CODEX_RUNTIME_HOME/auth.json" || return 1
  chmod 600 "$CODEX_RUNTIME_HOME/auth.json" || return 1
  if [ -x "$SCRIPT_DIR/codex-model-sync.sh" ]; then
    "$SCRIPT_DIR/codex-model-sync.sh" "$CODEX_RUNTIME_HOME" || {
      echo "gsd-run: Codex model materialization failed before the drive started" >&2
      return 1
    }
  fi
  ROLE_CONFIG_HASH="$(sha256_tree "$CODEX_RUNTIME_HOME/agents")" || return 1
  # Hash the immutable source manifest and hook bundle. The staged hooks.json
  # embeds CODEX_RUNTIME_HOME's random temporary path, so hashing the rewritten
  # copy would manufacture resume drift on every otherwise-identical launch.
  BUNDLE_HASH="$(sha256_tree "$source_root/gsd-file-manifest.json" \
    "$trusted_package/hooks" "$source_root/hooks.json")" || return 1
  FFS_SKILL_HASH="$(compute_ffs_skill_hash)" || return 1
}

consume_danger_grant() {
  local store output operation=consume
  [ "$REQUESTED_SANDBOX_MODE" = danger-full-access ] || return 0
  store="$DANGER_GRANT_STORE_FIXED"
  [ "$RESUME_REQUESTED" = 1 ] && operation=resume
  output="$(/usr/bin/python3 "$SCRIPT_DIR/consume-danger-grant.py" "$operation" \
    "$store" "$RUN_ID" "$GIT_COMMON_DIR" "$GSD_SKILL_NAME" "$NETWORK_MODE")" || {
    echo "gsd-run: refusing danger-full-access without a fresh exact run-bound <=72h sandbox:danger-full-access grant" >&2
    return 78
  }
  SANDBOX_GRANT_CONSUMPTION="$output"
}

persist_prelaunch_tuple() {
  local model="$1" effort="$2" tmp
  tmp="$(mktemp "$RUN_STATE_DIR/.gsd-run.tuple.XXXXXX")" || return 1
  {
    printf 'schema=ffs.gsd-run/v1\n'
    printf 'run_id=%s\n' "$RUN_ID"
    printf 'runtime=%s\n' "$SELECTED_HOST"
    printf 'codex_cli_version=%s\n' "${CODEX_CLI_VERSION:-none}"
    printf 'model=%s\n' "$model"
    printf 'effort=%s\n' "${effort:-none}"
    printf 'model_request_kind=%s\n' "$MODEL_REQUEST_KIND"
    printf 'model_request_name=%s\n' "${MODEL_REQUEST_NAME:-none}"
    printf 'model_request_id=%s\n' "${MODEL_REQUEST_ID:-none}"
    printf 'skill=%s\n' "$GSD_SKILL_NAME"
    printf 'skill_hash=%s\n' "$SKILL_HASH"
    printf 'role_config_hash=%s\n' "${ROLE_CONFIG_HASH:-none}"
    printf 'bundle_hash=%s\n' "${BUNDLE_HASH:-none}"
    printf 'ffs_skill_hash=%s\n' "${FFS_SKILL_HASH:-none}"
    printf 'auth_initial_hash=%s\n' "${CODEX_AUTH_INITIAL_HASH:-none}"
    printf 'sandbox_mode=%s\n' "$([ "$SELECTED_HOST" = codex ] && printf '%s' "$REQUESTED_SANDBOX_MODE" || printf host-native)"
    printf 'network_mode=%s\n' "$NETWORK_MODE"
    printf 'network_purpose=%s\n' "${NETWORK_PURPOSE:-none}"
    printf 'worktree_root=%s\n' "$RUN_WORKTREE_ROOT"
    printf 'adversary_degraded=%s\n' "$ADVERSARY_DEGRADED"
    printf 'sandbox_grant_consumption=%s\n' "$SANDBOX_GRANT_CONSUMPTION"
  } > "$tmp"
  if [ "$RESUME_REQUESTED" = 1 ]; then
    if [ ! -f "$RUN_TUPLE_FILE" ]; then
      rm -f "$tmp"
      echo "gsd-run: resume requested but no prelaunch tuple exists" >&2
      return 78
    fi
    # OAuth is intentionally writable during a run. A legitimate refresh may
    # change the next launch's initial hash; it is audit metadata, not runtime
    # drift. Every other tuple field remains resume-critical.
    if ! diff -q \
      <(grep -v '^auth_initial_hash=' "$RUN_TUPLE_FILE") \
      <(grep -v '^auth_initial_hash=' "$tmp") >/dev/null; then
      echo "gsd-run: resume tuple drift; refusing to launch with changed runtime/model/CLI/skill/sandbox" >&2
      diff -u "$RUN_TUPLE_FILE" "$tmp" >&2 || true
      rm -f "$tmp"
      return 78
    fi
    rm -f "$tmp"
  else
    atomic_replace "$tmp" "$RUN_TUPLE_FILE"
  fi
}

# A probe is deliberately independent of task text and read-only. Any failure
# here means the host cannot be admitted for a new drive; no output-substring
# classification is used. The stateful invocation below is a separate call.
probe_host() {
  local kind="$1" bin output rc model effort preferred_model preferred_effort
  if [ "$kind" = "codex" ]; then
    bin="${CODEX_BIN:-codex}"
    command -v "$bin" >/dev/null 2>&1 || return 127
    require_supported_codex_cli "$bin" || return $?
    preferred_model="$(codex_lead_model)"
    preferred_effort="$(codex_lead_effort)"
  else
    bin="${CLAUDE_BIN:-claude}"
    command -v "$bin" >/dev/null 2>&1 || return 127
    preferred_model="$(claude_lead_model)"
    preferred_effort=""
  fi
  command_surface_available "$kind" || return $?

  while IFS='|' read -r model effort; do
    if [ "$kind" = "codex" ]; then
      output="$(run_bounded "$PROBE_TIMEOUT_SECS" env -u OPENAI_API_KEY "$bin" exec \
        -c "model=\"$model\"" -c "model_reasoning_effort=\"$effort\"" \
        --sandbox read-only --ephemeral --ignore-user-config --ignore-rules \
        --color never "$PROBE_PROMPT" </dev/null 2>&1)"
      rc=$?
    else
      output="$(run_bounded "$PROBE_TIMEOUT_SECS" env \
        -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
        "$bin" --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
        --permission-mode plan --tools '' --no-session-persistence \
        --model "$model" -p "$PROBE_PROMPT" </dev/null 2>&1)"
      rc=$?
    fi
    record_probe_output "$kind" "$rc" "$output"
    if [ "$rc" -eq 0 ] && printf '%s\n' "$output" | grep -qx "$PROBE_MARKER"; then
      command_surface_available "$kind" || return $?
      if [ "$kind" = "codex" ]; then
        SELECTED_CODEX_MODEL="$model"
        SELECTED_CODEX_EFFORT="$effort"
      else
        SELECTED_CLAUDE_MODEL="$model"
      fi
      if [ "$model" != "$preferred_model" ]; then
        record_probe_note "$(host_label "$kind") model $preferred_model unavailable; selected $model before launch"
      fi
      return 0
    fi
    if [ "$rc" -eq 0 ]; then
      record_probe_note "$(host_label "$kind") probe missing acknowledgement '$PROBE_MARKER' for model $model"
      rc=1
    fi
    # A wall timeout implicates the CLI rather than one model. Do not repeat
    # the same dead binary for each tier; preserve time for the other vendor.
    [ "$rc" -ne 124 ] || return 124
  done < <(
    if [ "$EXACT_MODEL_REQUEST" -eq 1 ]; then
      printf '%s|%s\n' "$preferred_model" "$preferred_effort"
    else
      adversary_model_ladder "$kind" "$preferred_model" "$preferred_effort"
    fi
  )
  return "${rc:-1}"
}

SELECTED_HOST="$ACTIVE_HOST"
probe_host "$ACTIVE_HOST"
_native_rc=$?
if [ "$_native_rc" -ne 0 ]; then
  if [ "$CODEX_PREFLIGHT_FATAL" -eq 1 ]; then
    exit "$_native_rc"
  fi
  if [ "$EXACT_MODEL_REQUEST" -eq 1 ] || [ "$NETWORK_MODE" = none ] || ! cross_vendor_fallback_enabled; then
    record_probe_note "cross-vendor fallback disabled; native $(host_label "$ACTIVE_HOST") probe failed rc=$_native_rc and no stateful drive started"
    exit "$_native_rc"
  fi
  ALTERNATE_HOST="$(alternate_host "$ACTIVE_HOST")"
  echo "gsd-run: native $(host_label "$ACTIVE_HOST") unavailable before launch (probe rc=$_native_rc); checking $(host_label "$ALTERNATE_HOST")" >&2
  probe_host "$ALTERNATE_HOST"
  _alternate_rc=$?
  if [ "$_alternate_rc" -eq 0 ]; then
    SELECTED_HOST="$ALTERNATE_HOST"
    ADVERSARY_DEGRADED=true
    echo "gsd-run: DEGRADED — selected $(host_label "$SELECTED_HOST") before launch; the drive will run once on that host" >&2
  else
    echo "gsd-run: no usable host before launch (native rc=$_native_rc, alternate rc=$_alternate_rc); no stateful drive started" >&2
    echo "gsd-run: restore either CLI/model quota, then resume this exact GSD command" >&2
    exit 69
  fi
fi

ensure_run_worktree || exit $?

first="$1"
shift
if [ "$SELECTED_HOST" = "codex" ]; then
  CODEX_BIN="${CODEX_BIN:-codex}"
  LEAD_MODEL="${SELECTED_CODEX_MODEL:-$(codex_lead_model)}"
  LEAD_EFFORT="${SELECTED_CODEX_EFFORT:-$(codex_lead_effort)}"
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
  consume_danger_grant || exit $?
  persist_prelaunch_tuple "$LEAD_MODEL" "$LEAD_EFFORT" || exit $?
  # Subscription-only: -u OPENAI_API_KEY mirrors the ANTHROPIC_* strip on the
  # claude branch below. Codex prefers an ambient API key over the logged-in
  # session, so an injected key would silently meter the whole drive.
  RUN=(env -u OPENAI_API_KEY CODEX_HOME="$CODEX_RUNTIME_HOME" "$CODEX_BIN" exec
    -c "model=\"$LEAD_MODEL\""
    -c "model_reasoning_effort=\"$LEAD_EFFORT\""
    --sandbox "$REQUESTED_SANDBOX_MODE"
    --color never
    "$CODEX_COMMAND")
else
  CLAUDE_BIN="${CLAUDE_BIN:-claude}"
  LEAD_MODEL="${SELECTED_CLAUDE_MODEL:-$(claude_lead_model)}"
  case "$first" in
    \$gsd-*) first="/${first#\$}" ;;
    /gsd-*) ;;
    *) echo "gsd-run: unsupported Claude GSD command: $first" >&2; exit 2 ;;
  esac
  CMD_STR="$first"
  [ "$#" -eq 0 ] || CMD_STR="$CMD_STR $*"
  SKILL_HASH="$(sha256_file "${GSD_CLAUDE_SKILLS_ROOT:-$HOME/.claude/skills}/$GSD_SKILL_NAME/SKILL.md")" || exit 1
  FFS_SKILL_HASH="$(compute_ffs_skill_hash)" || exit 1
  persist_prelaunch_tuple "$LEAD_MODEL" "" || exit $?
  CLAUDE_ARGS=(--strict-mcp-config --mcp-config '{"mcpServers":{}}'
    --permission-mode acceptEdits --model "$LEAD_MODEL" -p "$CMD_STR")
  RUN=(env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN
    "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}")
fi

# STATEFUL BOUNDARY: exactly one drive is launched. Its output is never mined
# for availability words and no failure/timeout is replayed on another vendor.
# The on-disk log gets per-line wall-clock stamps (terminal stream stays raw)
# so phase durations are measurable after the fact — untimed logs made every
# "why is this phase slow" question unanswerable.
write_run_status running
cd "$RUN_WORKTREE_ROOT" || exit 1
run_bounded "$TIMEOUT_SECS" "${RUN[@]}" </dev/null 2>&1 \
  | tee >(perl -MPOSIX=strftime -pe '$|=1; print strftime("[%Y-%m-%dT%H:%M:%S] ", localtime)' > "$LOG_FILE")
rc="${PIPESTATUS[0]}"
if [ "$rc" -ne 0 ]; then
  echo "gsd-run: stateful drive failed on $SELECTED_HOST (rc=$rc); cross-vendor replay is forbidden" >&2
  echo "gsd-run: fix availability if needed, then resume on $SELECTED_HOST from .planning state; log: $LOG_FILE" >&2
fi
exit "$rc"
