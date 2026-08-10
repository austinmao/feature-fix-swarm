#!/usr/bin/env bash
# Host-native headless GSD runner. A fixed, read-only capability probe chooses
# a usable vendor BEFORE the stateful drive starts. Once started, a drive is
# never replayed on another vendor: nonzero/timeout returns resume guidance.
# Usage: gsd-run.sh <slash-command> [args...]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib-lock.sh"
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

# A drive launched by this runner carries GSD_ACTIVE_DRIVE=1. A headless
# agent inside that drive re-invoking gsd-run.sh (observed on spec-008
# phase 1: the codex agent re-ran the runner instead of executing the
# phase workflow, then read the single-flight refusal as a blocker —
# sandboxed kill -0 cannot see the live outer runner, so it looked like a
# dead-pid-with-live-heartbeat foreign owner) gets an INSTRUCTIVE refusal
# before any gate, probe, or state mutation, instead of a confusing lease
# error.
if [ "${GSD_ACTIVE_DRIVE:-0}" = "1" ]; then
  echo "gsd-run: NESTED-INVOCATION refused — this shell is already inside the active runner's drive session. The runner has already run the ownership gate and plan wall for this phase; execute the phase workflow directly per the skill instructions (spawn the plan executors). Never re-invoke gsd-run.sh from inside a drive." >&2
  exit 64
fi

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
  # spec-004 AC-005/INT-001: blocking per-phase plan review wall — every
  # plan under this phase must clear before the executor spawns. PHASE_DIR
  # resolution mirrors requirement-ownership-gate.sh's regex (^0*{N}-) so
  # both levers agree on which directory owns phase "$2" (ownership gate
  # already proved exactly one such directory exists, above).
  PLAN_WALL_LEVER="$SCRIPT_DIR/plan-wall.sh"
  if [ ! -f "$PLAN_WALL_LEVER" ]; then
    echo "gsd-run: plan wall lever missing: $PLAN_WALL_LEVER" >&2
    exit 78
  fi
  WALL_PHASE_DIR="$(python3 - "$REPO_ROOT" "$2" <<'PY'
import re, sys
from pathlib import Path
root, phase_number = sys.argv[1], int(sys.argv[2], 10)
phases_root = Path(root) / ".planning" / "phases"
dirs = sorted(p for p in phases_root.glob("*-*") if p.is_dir() and re.match(rf"^0*{phase_number}-", p.name))
print(dirs[0] if dirs else "", end="")
PY
)"
  wall_resolve_rc=$?
  # $2 is already proven numeric (requirement-ownership-gate.sh above would
  # have exited nonzero otherwise) and the ownership gate already proved
  # exactly one phase directory owns it — so a resolution failure or an
  # empty result here is a real bug, not a "no wall needed" case. Fail loud
  # rather than silently letting the phase start unwalled (spec-004 fix
  # round finding 12).
  if [ "$wall_resolve_rc" -ne 0 ]; then
    echo "gsd-run: FATAL: phase directory resolution for phase $2 failed (rc=$wall_resolve_rc) — refusing to start unwalled" >&2
    exit 78
  fi
  if [ -z "$WALL_PHASE_DIR" ]; then
    echo "gsd-run: FATAL: no phase directory found for phase $2 under .planning/phases (ownership gate proved one exists) — refusing to start unwalled" >&2
    exit 78
  fi
  GSD_PHASE_ID="$(basename "$WALL_PHASE_DIR")"
  export GSD_PHASE_ID
  bash "$PLAN_WALL_LEVER" "$WALL_PHASE_DIR" || exit $?
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
tier = {"frontier": "fable", "judgment": "opus", "execution": "sonnet", "volume": "haiku"}.get(name, model)
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
  && grep -q "^skill=$GSD_SKILL_NAME$" "$RUN_STATUS_FILE" \
  && grep -q "^skill=$GSD_SKILL_NAME$" "$RUN_TUPLE_FILE"; then
  # The tuple is what resume validates against; a tuple persisted by a
  # DIFFERENT skill's drive is definitionally not this drive's resume state.
  # Without this guard a pre-launch refusal (which never persists a tuple)
  # arms resume against the previous drive's tuple and wedges every fresh
  # launch on tuple drift.
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

# Coord identity/config (P-22..P-31). RUN_COORD_ID is the claim key and is
# set ONLY when the caller exported an explicit GSD_RUN_ID -- the auto-derived
# date+pid RUN_ID (above) is unique per invocation by construction, so
# claiming it could never return CLAIM-HELD and would buy zero collision
# protection while showing the run as coordinated in `coord.py status`. When
# set, RUN_COORD_ID is RUN_ID VERBATIM: never truncated, padded, lowercased
# or otherwise rewritten to fit coord.py's own CLAIM_ID_RE (64 bytes,
# alphanumeric-anchored) -- gsd-run.sh:246-251 above already proves RUN_ID
# fits ITS OWN [A-Za-z0-9_.-]{1,128} superset, and coord.py's exit 2
# propagates verbatim through the pre-existing `acquire_run_state || exit $?`
# at the bottom of this file rather than being silently repaired here.
RUN_COORD_GENERATION=""
RUN_COORD_TTL=300
COORD_PY="$SCRIPT_DIR/../coord/coord.py"
RUN_COORD_ID=""
[ -z "${GSD_RUN_ID:-}" ] || RUN_COORD_ID="$RUN_ID"
# Exported only on coordinated runs (P4-W5): an uncoordinated drive's child
# env stays byte-identical to pre-coordination behavior.
if [ -n "$RUN_COORD_ID" ]; then
  export FFS_RUN_ID="$RUN_ID"
  # The long-lived runner process is the claim's anchor, never a transient
  # command-substitution subshell (which os.getppid() would otherwise resolve
  # to and which is already dead by the time a peer checks staleness).
  export FFS_COORD_ANCHOR_PID="$$"
fi

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
RUNSTORE_ID=""
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

run_state_cli() {
  PYTHONPATH="$SCRIPT_DIR/../../lib${PYTHONPATH:+:$PYTHONPATH}" python3 -m run_state.cli "$@"
}

budget_prepare_mapping() {
  [ -n "${GSD_RUN_ID:-}" ] || return 0
  # Budget/mapping semantics exist only for LEDGER-shaped run ids (must
  # mirror gates.py LEDGER_RUN_ID_PAT). A fixture name or this script's own
  # date-PID fallback id has no grant/budget ledger, so there is nothing to
  # map or account — skip silently rather than refusing the drive (first
  # integration run: every non-ledger run exited 78 on
  # RUN-MAPPING-REJECTED). Run-state/mapping failure for a VALID ledger id
  # below stays fail-closed.
  [[ "$GSD_RUN_ID" =~ ^(spec-[0-9]{3}|adhoc-[a-z0-9][a-z0-9-]*|run-[0-9]+)$ ]] || return 0
  local started gates existing
  gates="$SCRIPT_DIR/../../lib/gates.py"
  # A relaunch of the same ledger run (deviation checkpoint, mid-phase
  # session end) reuses the mapped runstore: one ledger run owns exactly one
  # runstore across drives, and token accounting stays cumulative. A fresh
  # record per drive would either die on RUN-MAPPING-CONFLICT (second drive
  # of phase 2 wedged on this) or fragment the budget across runstores.
  if existing="$(python3 "$gates" map-run --ledger-run-id "$GSD_RUN_ID" --get 2>/dev/null)" && [ -n "$existing" ]; then
    local record
    if record="$(run_state_cli status "$existing" 2>/dev/null)"; then
      # A runstore that already crossed its budget must not launch another
      # drive: the crossing wrote its BUDGET-BREACH/quarantine mark, and the
      # cwd-relative status file cannot carry that refusal across checkouts.
      # The durable used-vs-budget comparison is the launch-time gate.
      if ! printf '%s' "$record" | python3 -c '
import json, sys
r = json.load(sys.stdin)
b, u = r.get("tokens_budget"), (r.get("tokens_used") or 0)
sys.exit(1 if (b is not None and u >= b) else 0)'; then
        echo "gsd-run: BUDGET-BREACHED: mapped runstore '$existing' has exhausted its token budget — refusing relaunch (quarantined)" >&2
        return 78
      fi
      RUNSTORE_ID="$existing"
      return 0
    fi
    echo "gsd-run: BUDGET-MAPPING-FAILED: mapped runstore '$existing' is unreadable — refusing a fresh start that would orphan its accounting" >&2
    return 78
  fi
  local -a args=(start --skill fix --objective "$GSD_SKILL_NAME" --worktree "$RUN_WORKTREE_ROOT")
  [ -z "${GSD_TOKEN_BUDGET:-}" ] || args+=(--tokens "$GSD_TOKEN_BUDGET")
  started="$(run_state_cli "${args[@]}" 2>&1)" || {
    echo "gsd-run: BUDGET-MAPPING-FAILED: cannot create run-state record" >&2; return 78; }
  RUNSTORE_ID="$(printf '%s' "$started" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' 2>/dev/null)" || return 78
  python3 "$gates" map-run --ledger-run-id "$GSD_RUN_ID" --runstore-id "$RUNSTORE_ID" >/dev/null || {
    echo "gsd-run: BUDGET-MAPPING-FAILED: cannot persist ledger mapping" >&2; return 78; }
}

budget_account_tail() {
  local capture="$1" tokens update rc
  [ -n "$RUNSTORE_ID" ] || return 0
  # Two anchored trailer shapes, last occurrence in the 10-line tail wins:
  # single-line 'tokens used: N' AND the live codex CLI's two-line form —
  # 'tokens used' followed by a comma-grouped count on the next line (the
  # colon-only parse WARNed BUDGET-ACCOUNTING-UNAVAILABLE on every real
  # drive). Mid-stream trailer-shaped text stays unread: only the tail.
  tokens="$(tail -n 10 "$capture" | python3 -c '
import re, sys
lines = [l.rstrip("\n") for l in sys.stdin]
val = None
for i, l in enumerate(lines):
    m = re.fullmatch(r"tokens used:?\s*([0-9][0-9,]*)?\s*", l)
    if not m:
        continue
    if m.group(1):
        val = m.group(1)
    elif i + 1 < len(lines):
        m2 = re.fullmatch(r"\s*([0-9][0-9,]*)\s*", lines[i + 1])
        if m2:
            val = m2.group(1)
print(val.replace(",", "") if val else "")
')"
  if [ -z "$tokens" ]; then
    echo "gsd-run: WARN BUDGET-ACCOUNTING-UNAVAILABLE: no parseable token trailer" >&2
    return 0
  fi
  update="$(run_state_cli update "$RUNSTORE_ID" --tokens "$tokens" 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ] || { [ -n "$update" ] && ! printf '%s\n' "$update" | grep -Eq '^BUDGET-BREACH: [0-9a-f]{12} [0-9]+ [0-9]+$'; }; then
    # Empty output is the normal non-breach result; malformed non-empty output
    # is observable but must not rewrite a successful drive outcome.
    if [ "$rc" -ne 0 ]; then echo "gsd-run: WARN BUDGET-ACCOUNTING-FAILED: cmd_update rc=$rc" >&2; fi
    return 0
  fi
  if printf '%s\n' "$update" | grep -q '^BUDGET-BREACH:'; then
    echo "gsd-run: BUDGET-BREACH: quarantining subsequent launches" >&2
    write_run_status quarantined 0 || return 1
  fi
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

# coord_claim_run (P-22, P-24, P-25, P-28). Called INSIDE acquire_run_state,
# after the run-state pidfile ownership confirmation and BEFORE the heartbeat
# subshell forks, so the fork's copy of RUN_COORD_GENERATION is already
# populated when the subshell is created (P-28's whole read-side proof).
coord_claim_run() {
  # FIRST: coord.py absent -> silent no-op, no stdout, no stderr (P-25
  # fail-soft). Probing RUN_COORD_ID first would print a new stderr line in
  # every coord-less repo and break "byte-identical to today".
  [ -f "$COORD_PY" ] || return 0
  # SECOND: coord layer present but no explicit GSD_RUN_ID -> no claim is
  # attempted, one stderr notice naming the remedy (P-22/P-23). The 31+
  # pre-existing cases run with no GSD_RUN_ID; this order keeps them silent.
  if [ -z "$RUN_COORD_ID" ]; then
    echo "gsd-run: automatic coord claim skipped (no explicit GSD_RUN_ID exported); export a spec-stable GSD_RUN_ID or claim manually per docs/coordination.md" >&2
    return 0
  fi
  local output rc
  output="$(python3 "$COORD_PY" claim "$RUN_COORD_ID" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    RUN_COORD_GENERATION="$(printf '%s\n' "$output" | sed -n 's/^CLAIM-OK generation=\([0-9]*\)$/\1/p' | head -1)"
    # ONE extra subprocess, at acquire only, never per tick: read the
    # claim's OWN ttl_secs (idempotent re-claims and manual --ttl grants
    # carry it forward) rather than assuming DEFAULT_TTL_SECS, which is
    # what P-24b's staleness budget below is measured against.
    local status_line claim_ttl
    status_line="$(python3 "$COORD_PY" status 2>/dev/null | grep -F "claim:$RUN_COORD_ID ")"
    claim_ttl="$(printf '%s\n' "$status_line" | sed -n 's/.*ttl_secs=\([0-9]*\).*/\1/p' | head -1)"
    if [ -n "$claim_ttl" ]; then
      RUN_COORD_TTL="$claim_ttl"
    else
      echo "gsd-run: could not read claim ttl_secs from coord status; using default ${RUN_COORD_TTL}s" >&2
    fi
    return 0
  fi
  # Never remap a coord exit code to gsd-run.sh's own 75, and never retry
  # with a modified id -- the caller's exact value already went to coord.py.
  echo "gsd-run: coord claim failed for $RUN_COORD_ID (rc=$rc): $output" >&2
  case "$rc" in
    2) echo "gsd-run: id rejected by coord.py's CLAIM_ID_RE (alphanumeric-anchored, 64-byte cap) -- shorten GSD_RUN_ID and retry; it is never truncated for you" >&2 ;;
    69) echo "gsd-run: coord store unavailable -- run 'python3 scripts/coord/coord.py doctor', set FFS_COORD_MODE=off, or 'python3 -m pip install --requirement requirements-dev.txt'" >&2 ;;
  esac
  return "$rc"
}

# coord_release_run (P-24). Called from cleanup_runner, on every exit path.
coord_release_run() {
  [ -f "$COORD_PY" ] || return 0
  # Guards implied ownership: RUN_COORD_GENERATION is non-empty only if
  # coord_claim_run succeeded, which only runs after acquire_run_state's
  # pidfile ownership confirmation succeeded -- so a second entry (double
  # cleanup_runner) or an entry by a run that never claimed is a no-op.
  [ -n "$RUN_COORD_GENERATION" ] || return 0
  python3 "$COORD_PY" release "$RUN_COORD_ID" --generation "$RUN_COORD_GENERATION" >/dev/null 2>&1 || true
  RUN_COORD_GENERATION=""
}

# coord_renew_run (P-24, P-24b, P-26, P-28). Called every heartbeat tick from
# INSIDE the existing heartbeat subshell -- one call serves both REQ-11's
# renew and revalidate clauses; no separate claim-check subprocess is added.
# Local function-return codes only -- NEVER a process exit code and NEVER
# coord.py's own 0/2/3/4/64/69/75/78 contract -- picked well outside that
# range so a later reader cannot mistake one for the process exit table:
#   0  = renew reached the store and succeeded
#   90 = DETECTED revocation (coord.py claim-renew returned 3 or 4)
#   91 = DEGRADED: any other coord.py failure, tolerated under P-24b's budget
# Collapsing 90/91 into a shared "return 0" would make the staleness budget
# below unmeasurable -- a tolerated failure must never look like a success.
coord_renew_run() {
  [ -f "$COORD_PY" ] || return 0
  # Empty generation only happens for a P-22 no-claim or P-25 no-coord run,
  # where the heartbeat still ticks but there is no claim to renew or go
  # stale -- never true for a claimed run, since Task 1's claim call site
  # runs before this subshell forks and the fork copies the populated value.
  [ -n "$RUN_COORD_GENERATION" ] || return 0
  local output rc
  output="$(python3 "$COORD_PY" claim-renew "$RUN_COORD_ID" --generation "$RUN_COORD_GENERATION" 2>&1)"
  rc=$?
  case "$rc" in
    0) return 0 ;;
    3|4)
      echo "gsd-run: CLAIM-SUPERSEDED renewing $RUN_COORD_ID (coord exit $rc)" >&2
      printf '%s\n' "$output" >&2
      return 90
      ;;
    *)
      echo "gsd-run: coord claim-renew warning for $RUN_COORD_ID (rc=$rc); tolerating within the staleness budget" >&2
      printf '%s\n' "$output" >&2
      return 91
      ;;
  esac
}

acquire_run_state() {
  [ ! -L "$RUN_STATE_DIR" ] || {
    echo "gsd-run: refusing symlinked run-state directory: $RUN_STATE_DIR" >&2
    return 78
  }
  mkdir -p "$RUN_STATE_DIR"
  ffs_lock_acquire "$RUN_PID_FILE" "$RUN_HEARTBEAT_FILE" "$RUN_RECLAIM_DIR" \
    "$RUN_MACHINE_ID" "${GSD_FOREIGN_LEASE_SECS:-120}" "${GSD_RECLAIM_LEASE_SECS:-30}" \
    "gsd-run" "$RUN_STATUS_FILE" || return $?
  RUN_STATE_OWNED=1

  local heartbeat_secs
  # Lock claim/reclaim/lease checks live exclusively in lib-lock.sh.
  write_heartbeat || return 1
  write_run_status probing
  heartbeat_secs="${GSD_HEARTBEAT_SECS:-15}"
  case "$heartbeat_secs" in ''|*[!0-9]*|0) heartbeat_secs=15 ;; esac
  # AFTER the pidfile ownership confirmation above (a losing pidfile
  # contender already returned 75 and never reaches here) and BEFORE the
  # heartbeat subshell forks below -- a claim taken after the fork would
  # leave the subshell's copy of RUN_COORD_GENERATION empty for the run's
  # whole life (P-28). `return $?` (never `exit`) lets the pre-existing
  # `acquire_run_state || exit $?` propagate coord.py's own code verbatim.
  coord_claim_run || return $?
  (
    heartbeat_sleep=""
    trap '[ -z "$heartbeat_sleep" ] || kill "$heartbeat_sleep" 2>/dev/null || true; exit 0' TERM INT
    # Subshell-local, seeded to fork time -- the subshell owns the whole
    # renew loop and is the only reader, so no write-back channel is needed
    # (P-28). Refreshed on every successful renew below; a tolerated (P-24b
    # DEGRADED) tick deliberately does NOT refresh it, which is what makes
    # the staleness budget measurable.
    _coord_last_renew_success="$(date +%s)"
    while kill -0 "$$" 2>/dev/null; do
      if ! write_heartbeat; then
        echo "gsd-run: heartbeat refresh failed; terminating drive rather than losing its lease" >&2
        kill -TERM "$$" 2>/dev/null || true
        exit 1
      fi
      # P-26: one claim-renew call serves both REQ-11's renew and revalidate
      # clauses; the 15s default tick is deliberately 4x the claim's own 60s
      # heartbeat assumption, so CLAIM-SUPERSEDED is detected strictly faster
      # than "at the next phase boundary". No second claim-check call.
      coord_renew_run
      _coord_renew_rc=$?
      if [ "$_coord_renew_rc" -eq 0 ]; then
        _coord_last_renew_success="$(date +%s)"
      elif [ "$_coord_renew_rc" -eq 90 ]; then
        printf 'CLAIM-SUPERSEDED\n' > "$RUN_STATE_DIR/gsd-run.coord-abort" 2>/dev/null || true
        echo "gsd-run: CLAIM-SUPERSEDED; terminating drive rather than continuing with a revoked claim" >&2
        # kill -TERM "$$" alone only queues the signal for the parent shell
        # and bash defers running its trap until the CURRENT foreground
        # command completes -- so while the stateful drive's pipeline
        # (run_bounded | tee) is actively running, plain "$$" would not be
        # dead until that pipeline finishes on its own, defeating the whole
        # point of a bounded kill. pkill -P "$$" targets ONLY $$'s direct
        # children (the running `timeout`/tee pipe members), never siblings
        # or ancestors, which lets that foreground pipeline unblock promptly
        # so the pending TERM trap on $$ (below) fires within this tick.
        # This heartbeat subshell is itself one of $$'s children, so the
        # pkill also TERMs *us* (P4-W4). That self-signal is benign by
        # ordering: both abort arms have already written their coord-abort
        # sentinel and diagnostic before the pkill line, and the very next
        # statements are kill+exit — dying to our own TERM a tick early is
        # exactly the shutdown we were about to perform.
        pkill -TERM -P "$$" 2>/dev/null || true
        kill -TERM "$$" 2>/dev/null || true
        exit 1
      elif [ -n "$RUN_COORD_GENERATION" ]; then
        # P-24b staleness budget: guarded on a non-empty generation so a
        # P-22 no-claim or P-25 no-coord run (whose heartbeat still ticks)
        # can never kill itself over a claim it never took.
        _coord_now="$(date +%s)"
        if [ $((_coord_now - _coord_last_renew_success)) -ge "$RUN_COORD_TTL" ]; then
          printf 'CLAIM-STALE\n' > "$RUN_STATE_DIR/gsd-run.coord-abort" 2>/dev/null || true
          echo "gsd-run: CLAIM-STALE; no successful claim-renew within ttl_secs=$RUN_COORD_TTL; terminating drive" >&2
          # See the CLAIM-SUPERSEDED arm above for why pkill -P is required
          # in addition to kill -TERM "$$" -- without it this arm cannot
          # meet its own "dead within ttl_secs + one tick" mandate while a
          # stateful drive is actively running in the foreground pipeline.
          pkill -TERM -P "$$" 2>/dev/null || true
          kill -TERM "$$" 2>/dev/null || true
          exit 1
        fi
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
  local rc="$?" auth_rc=0
  trap - EXIT
  if [ -n "$RUN_HEARTBEAT_PID" ]; then
    kill "$RUN_HEARTBEAT_PID" 2>/dev/null || true
    wait "$RUN_HEARTBEAT_PID" 2>/dev/null || true
  fi
  cleanup_codex_runtime || auth_rc=$?
  # Every exit path unwinds through this trap (gsd-run.sh:614), so this is
  # the one release site for normal, non-zero, timeout, external SIGTERM and
  # the mid-run coord self-kill alike (P-24). The heartbeat subshell has
  # already been killed above, so no renew can race this release.
  coord_release_run
  if [ "$auth_rc" -ne 0 ]; then
    echo "gsd-run: OAuth refresh synchronization failed (rc=$auth_rc)" >&2
    [ "$rc" -ne 0 ] || rc="$auth_rc"
  fi
  if [ "$RUN_STATE_OWNED" -eq 1 ]; then
    write_run_status "$([ "$rc" -eq 0 ] && echo completed || echo failed)" "$rc"
    # Additive, after the write above (which atomically REPLACES the status
    # file) -- a coord-abort token written from the heartbeat subshell would
    # otherwise be clobbered by that replace. One-token sidecar, not a
    # general channel, and never a route for RUN_COORD_GENERATION.
    if [ -f "$RUN_STATE_DIR/gsd-run.coord-abort" ]; then
      printf 'coord_abort=%s\n' "$(cat "$RUN_STATE_DIR/gsd-run.coord-abort" 2>/dev/null)" >> "$RUN_STATUS_FILE"
      rm -f "$RUN_STATE_DIR/gsd-run.coord-abort"
    fi
    ffs_lock_release "$RUN_PID_FILE" "$RUN_MACHINE_ID" || true
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
  printf '%s\n' "$USER_AGENTS_ROOT_FIXED/skills"
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
  [ "$major" -eq 0 ] && [ "$minor" -ge 137 ] && [ "$minor" -lt 148 ]
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
    echo "gsd-run: Codex CLI ${version:-unknown} is outside supported range >=0.137.0,<0.148.0" >&2
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
  local kind="$1" root skill_root manifest project_skill
  if [ "$kind" = "codex" ]; then
    root="$(codex_source_root)"
    skill_root="$(codex_skill_root)"
    for project_skill in "$REPO_ROOT"/.agents/skills/gsd-*; do
      if [ -e "$project_skill" ] || [ -L "$project_skill" ]; then
        echo "gsd-run: project-local GSD skill overrides are forbidden; remove $project_skill" >&2
        return 78
      fi
    done
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
    /usr/bin/python3 "$SCRIPT_DIR/stage-gsd-skills.py" verify \
      "$manifest" "$skill_root" "$GSD_SKILL_NAME" || return $?
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
  local source_root skill_root auth_source network_bool writable_json trusted_package node_bin auth_meta auth_uid auth_mode
  source_root="$(codex_source_root)"
  skill_root="$(codex_skill_root)"
  command_surface_available codex || return $?
  CODEX_RUNTIME_HOME="$(mktemp -d "${TMPDIR:-/tmp}/ffs-gsd-codex.XXXXXX")" || return 1
  mkdir -p "$CODEX_RUNTIME_HOME/skills"
  # The upstream installer owns GSD skills in the global `.agents` root and
  # records their exact hashes in the Codex manifest. Project overrides and
  # symlink-following copies are intentionally excluded from headless runs.
  /usr/bin/python3 "$SCRIPT_DIR/stage-gsd-skills.py" stage \
    "$source_root/gsd-file-manifest.json" "$skill_root" \
    "$CODEX_RUNTIME_HOME/skills" "$GSD_SKILL_NAME" || return $?
  SKILL_HASH="$(sha256_tree "$CODEX_RUNTIME_HOME/skills")" || return 1
  trusted_package="$(trusted_gsd_package_root)" || return $?
  node_bin="$(trusted_node_bin)" || return $?
  /usr/bin/python3 "$SCRIPT_DIR/codex-runtime-bundle.py" \
    "$source_root" "$trusted_package" "$node_bin" "$CODEX_RUNTIME_HOME" "$RUN_WORKTREE_ROOT" || return $?

  network_bool=false
  [ "$NETWORK_MODE" = enabled ] && network_bool=true
  # P-29: also grants the shared .feature-fix-swarm subtree at the main
  # checkout (git-common-dir's parent), which is where coord.py's store
  # anchors (coord.py:180-189) -- NOT the run worktree -- so a headless
  # sandboxed drive's coord writes were unconditionally denied before this
  # line. The whole subtree is granted, not just coord/: it is gitignored
  # (no tracked source becomes writable) and this also unblocks the sibling
  # evidence.json write (lib/gates.py:1372) that was broken for the same
  # reason.
  # Spec-008 live fix: a commit inside the LINKED worktree writes git
  # metadata OUTSIDE the worktree root — its per-worktree git dir
  # (<common>/worktrees/<run-id>: index.lock, HEAD, logs) and the SHARED
  # object store (<common>/objects). Without these two roots every
  # sandboxed `git add`/`git commit` dies with "Unable to create
  # index.lock: Operation not permitted" and the drive cannot make its
  # RED/GREEN/SUMMARY commits. Deliberately NARROW: never the whole .git —
  # hooks/ (arbitrary code executed by the next unsandboxed git call) and
  # refs/ (other branches) stay non-writable.
  # The executor also creates/advances phase branches under the gsd/*
  # namespace (refs/heads/gsd/phase-* + their reflogs). Grant EXACTLY that
  # namespace — pre-created here because the sandbox cannot mkdir inside
  # the ungranted refs/heads parent — so main and every other branch ref
  # stay non-writable.
  mkdir -p "$GIT_COMMON_DIR/refs/heads/gsd" "$GIT_COMMON_DIR/logs/refs/heads/gsd" || return 1
  writable_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
    "$RUN_WORKTREE_ROOT" "$PROJECT_PRIMARY_ROOT/.feature-fix-swarm" \
    "$GIT_COMMON_DIR/objects" "$GIT_COMMON_DIR/worktrees/$RUN_ID" \
    "$GIT_COMMON_DIR/refs/heads/gsd" "$GIT_COMMON_DIR/logs/refs/heads/gsd")" || return 1
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
budget_prepare_mapping || exit $?

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
  RUN=(env -u OPENAI_API_KEY GSD_ACTIVE_DRIVE=1 CODEX_HOME="$CODEX_RUNTIME_HOME" "$CODEX_BIN" exec
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
  RUN=(env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN GSD_ACTIVE_DRIVE=1
    "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}")
fi

# STATEFUL BOUNDARY: exactly one drive is launched. Its output is never mined
# for availability words and no failure/timeout is replayed on another vendor.
# The on-disk log gets per-line wall-clock stamps (terminal stream stays raw)
# so phase durations are measurable after the fact — untimed logs made every
# "why is this phase slow" question unanswerable.
write_run_status running
cd "$RUN_WORKTREE_ROOT" || exit 1
RESPAWN_MAX="${FFS_RESPAWN_MAX:-1}"
RESPAWN_MIN_SECS="${FFS_RESPAWN_MIN_SECS:-600}"
RESPAWN_BASE_SHA="$(git -C "$RUN_WORKTREE_ROOT" rev-parse HEAD 2>/dev/null || true)"
RESPAWN_STARTED="$SECONDS"
attempt=1
attempt_timeout="$TIMEOUT_SECS"
: > "$LOG_FILE"
while :; do
  DRIVE_CAPTURE="$(mktemp "${TMPDIR:-/tmp}/ffs-gsd-drive.XXXXXX")" || exit 1
  run_bounded "$attempt_timeout" "${RUN[@]}" </dev/null 2>&1 \
    | tee >(perl -MPOSIX=strftime -pe '$|=1; print strftime("[%Y-%m-%dT%H:%M:%S] ", localtime)' >> "$LOG_FILE") \
    | tee "$DRIVE_CAPTURE"
  rc="${PIPESTATUS[0]}"
  budget_account_tail "$DRIVE_CAPTURE" || { rm -f "$DRIVE_CAPTURE"; exit 1; }
  rm -f "$DRIVE_CAPTURE"
  [ "$rc" -eq 0 ] && break
  [ "$attempt" -le "$RESPAWN_MAX" ] || break
  grep -qx 'state=quarantined' "$RUN_STATUS_FILE" 2>/dev/null && break
  should_respawn=0
  if [ "$rc" -eq 124 ]; then
    should_respawn=1
  elif [ -n "$RESPAWN_BASE_SHA" ]; then
    commit_count="$(git -C "$RUN_WORKTREE_ROOT" rev-list --count "$RESPAWN_BASE_SHA"..HEAD 2>/dev/null)" || commit_count=error
    [[ "$commit_count" =~ ^[0-9]+$ ]] && [ "$commit_count" -eq 0 ] && should_respawn=1
  fi
  [ "$should_respawn" -eq 1 ] || break
  # If this run has a lifecycle record, charge the same durable respawn
  # budget that reconcile.sh uses.  Older/direct gsd-run invocations have no
  # such record and retain their established one-retry behaviour.
  # lifecycle.sh resolves its record dir from its CWD's git toplevel, so the
  # record lives under the run worktree (executor checkpoints) or this repo
  # root (orchestration-side checkpoints) — never under RUN_STATE_DIR.
  lifecycle_root=""
  for _lc_root in "$RUN_WORKTREE_ROOT" "$REPO_ROOT"; do
    [ -n "$_lc_root" ] && [ -f "$_lc_root/.planning/run-state/lifecycle-$RUN_ID.json" ] && { lifecycle_root="$_lc_root"; break; }
  done
  if [ -n "$lifecycle_root" ]; then
    if ! _dec_err="$(cd "$lifecycle_root" && bash "$SCRIPT_DIR/lifecycle.sh" decrement "$RUN_ID" respawns 2>&1 >/dev/null)"; then
      if [[ "$_dec_err" == *budget-exhausted* ]]; then
        (cd "$lifecycle_root" && bash "$SCRIPT_DIR/lifecycle.sh" transition "$RUN_ID" failed respawn-budget-exhausted >/dev/null 2>&1) || true
        echo "GSD-RUN:RESPAWN budget-exhausted run=$RUN_ID" >&2
      else
        echo "GSD-RUN:RESPAWN lifecycle-error run=$RUN_ID" >&2
      fi
      break
    fi
  fi
  echo "GSD-RUN:RESPAWN attempt=$((attempt + 1))/$((RESPAWN_MAX + 1)) rc=$rc" >&2
  printf 'GSD-RUN:RESPAWN attempt=%s/%s rc=%s\n' "$((attempt + 1))" "$((RESPAWN_MAX + 1))" "$rc" >> "$LOG_FILE"
  attempt=$((attempt + 1))
  elapsed=$((SECONDS - RESPAWN_STARTED))
  remaining=$((TIMEOUT_SECS - elapsed))
  [ "$remaining" -gt 0 ] || remaining=0
  attempt_timeout="$remaining"
  [ "$attempt_timeout" -ge "$RESPAWN_MIN_SECS" ] || attempt_timeout="$RESPAWN_MIN_SECS"
  [ "$attempt_timeout" -le "$TIMEOUT_SECS" ] || attempt_timeout="$TIMEOUT_SECS"
done
if [ "$rc" -ne 0 ]; then
  echo "gsd-run: stateful drive failed on $SELECTED_HOST (rc=$rc); cross-vendor replay is forbidden" >&2
  echo "gsd-run: fix availability if needed, then resume on $SELECTED_HOST from .planning state; log: $LOG_FILE" >&2
fi
exit "$rc"
