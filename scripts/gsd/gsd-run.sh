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

# Release B opt-in: a managed run never falls back to this legacy runner. The
# managed ingress is `scripts/gsd/ffs-frontend.sh <frontend>` (frontend-start),
# which binds the private runtime descriptor and the sealed lifecycle before the
# first stateful write. Disabling the opt-in affects only future runs.
if [ -n "${FFS_MANAGED_INGRESS:-}" ] && [ "${FFS_MANAGED_INGRESS}" != "0" ]; then
  echo "gsd-run: FFS_MANAGED_INGRESS is set; refusing the legacy runner. Use scripts/gsd/ffs-frontend.sh <feature-spec|fix|code-uplift|feature-implement|task-swarm> with FFS_UPSTREAM_RUNTIME_MANIFEST/SHA256 (see run_state.cli describe-upstream-runtime)." >&2
  exit 78
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

# ── wall rc-3 bounded auto-continue ─────────────────────────────────────────
# plan-wall.sh exit 3 (WALL-ROUND-CAP) is TERMINAL by
# default. This runner IS the --autonomous headless path — a bare `|| exit`
# kills the process before any agent turn could apply the operator unblock
# recipe plan-wall itself prints (resolve findings -> loop-round --reset ->
# re-run). When that recipe's precondition is machine-verifiably met (zero
# unresolved CRITICAL wall findings for the phase — residual HIGHs are OPEN
# by design under the one-round wall's PASS-RESIDUAL, so they must never
# block the recipe) AND the operator
# pre-granted `wall-reset:<phase-slug>` in the run's autonomy ledger, run the
# recipe here, exactly once per phase per run:
#   bounded by the durable `wall-autoreset:<slug>` loop-round counter
#   (PLAN_WALL_AUTO_RESET_MAX, default 1 — never replenished mid-run; only
#   the run finalizer's loop sweep clears it), and the budget is spent
#   regardless of the re-run's outcome; raising PLAN_WALL_AUTO_RESET_MAX is
#   a deliberate, visible escape mirroring PLAN_WALL_MAX_ROUNDS — never a
#   silent off-switch. ANY nonzero rc from the re-run is
#   quarantine-terminal — post-reset the wall restarts at round 1 of its
#   one-round policy: a pass (including PASS-RESIDUAL) clears, a fresh
#   CRITICAL surfaces as rc 1 and must terminate or the uncounted
#   wall->fix->wall loop this cap exists to prevent would restart. No grant (interactive sessions never mint one) =
#   every skip path falls through to the unchanged quarantine, fail-closed.
_gsd_run_wall_gate() {
  local phase_dir="$1" slug rc gates_py c open plan_rel n f
  bash "$PLAN_WALL_LEVER" "$phase_dir"
  rc=$?
  [ "$rc" -eq 3 ] || return "$rc"
  slug="$(basename "$phase_dir")"
  if [ -z "${GSD_RUN_ID:-}" ]; then
    echo "gsd-run: WALL-AUTO-CONTINUE skipped (no GSD_RUN_ID) — quarantine stands" >&2
    return "$rc"
  fi
  gates_py=""
  for c in \
    "$REPO_ROOT/packages/feature-fix-swarm/lib/gates.py" \
    "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
    "$REPO_ROOT/lib/gates.py"; do
    [ -f "$c" ] && gates_py="$c" && break
  done
  if [ -z "$gates_py" ]; then
    echo "gsd-run: WALL-AUTO-CONTINUE skipped (gates.py not found) — quarantine stands" >&2
    return "$rc"
  fi
  # Zero unresolved CRITICAL wall findings across every plan in the phase —
  # the wall's blocking severity. HIGHs are deliberately excluded: under the
  # one-round wall they ride as PASS-RESIDUAL assumptions and are open on
  # every pass-residual phase, so counting them would make auto-continue
  # permanently unreachable. A failed,
  # unparseable, or non-numeric queue answer counts as an open finding, and
  # a phase with NO enumerable plan files skips too: zero plans checked is
  # zero evidence, not a pass (fail-closed).
  local plans=0
  open=0
  for f in "$phase_dir"/*-PLAN.md "$phase_dir"/PLAN.md; do
    [ -f "$f" ] || continue
    plans=$((plans + 1))
    plan_rel="${f#"$REPO_ROOT"/}"
    n="$(python3 "$gates_py" findings-queue list --unresolved --source wall \
      --severity CRITICAL --plan "$plan_rel" 2>/dev/null \
      | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null)"
    case "$n" in ''|*[!0-9]*) n=1 ;; esac
    open=$((open + n))
  done
  if [ "$plans" -eq 0 ]; then
    echo "gsd-run: WALL-AUTO-CONTINUE skipped (phase=$slug has no enumerable plan files — nothing verified) — quarantine stands" >&2
    return "$rc"
  fi
  if [ "$open" -ne 0 ]; then
    echo "gsd-run: WALL-AUTO-CONTINUE skipped (phase=$slug has $open unresolved CRITICAL finding(s)) — quarantine stands" >&2
    return "$rc"
  fi
  if ! python3 "$gates_py" check-grant "$GSD_RUN_ID" --action "wall-reset:$slug" >/dev/null 2>&1; then
    echo "gsd-run: WALL-AUTO-CONTINUE skipped (no wall-reset:$slug grant for run $GSD_RUN_ID) — quarantine stands" >&2
    return "$rc"
  fi
  local lr_rc=0
  python3 "$gates_py" loop-round "$GSD_RUN_ID" "wall-autoreset:$slug" \
      --max "${PLAN_WALL_AUTO_RESET_MAX:-1}" >/dev/null 2>&1 || lr_rc=$?
  if [ "$lr_rc" -ne 0 ]; then
    if [ "$lr_rc" -eq 1 ]; then
      echo "gsd-run: WALL-AUTO-CONTINUE skipped (autoreset budget spent for phase=$slug) — quarantine stands" >&2
    else
      echo "gsd-run: WALL-AUTO-CONTINUE skipped (autoreset counter store unusable, rc=$lr_rc) — quarantine stands" >&2
    fi
    return "$rc"
  fi
  echo "gsd-run: WALL-AUTO-CONTINUE phase=$slug — zero unresolved findings, wall-reset:$slug granted; resetting wall round and re-running once" >&2
  if ! python3 "$gates_py" loop-round "$GSD_RUN_ID" "wall:$slug" --reset --max 1 >/dev/null 2>&1; then
    echo "gsd-run: WALL-AUTO-CONTINUE aborted (wall round-counter reset failed) — quarantine stands" >&2
    return "$rc"
  fi
  bash "$PLAN_WALL_LEVER" "$phase_dir"
  rc=$?
  [ "$rc" -eq 0 ] || echo "gsd-run: WALL-AUTO-CONTINUE exhausted (re-run rc=$rc) — quarantine terminal" >&2
  return "$rc"
}

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
  _gsd_run_wall_gate "$WALL_PHASE_DIR" || exit $?
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
GSD_RESUME_EXPLICIT=0
[ "${GSD_RESUME+x}" != x ] || GSD_RESUME_EXPLICIT=1
case "${GSD_RESUME:-}" in
  ""|0|1) ;;
  *) echo "gsd-run: GSD_RESUME must be 0 or 1 when set" >&2; exit 2 ;;
esac
# Fresh-start recovery values authorize only prelaunch tuple replacement.
# Consume their exported inputs before a host probe or stateful drive can
# inherit them; the private copies below are intentionally non-exported.
unset FRESH_START_REASON FRESH_START_EXPECTED_ROLE_CONFIG_HASH \
  FRESH_START_EXPECTED_BUNDLE_HASH FRESH_START_NEW_BUNDLE_HASH
FRESH_START_REASON="${GSD_FRESH_START_REASON:-}"
FRESH_START_EXPECTED_ROLE_CONFIG_HASH="${GSD_FRESH_START_EXPECTED_ROLE_CONFIG_HASH:-}"
FRESH_START_EXPECTED_BUNDLE_HASH="${GSD_FRESH_START_EXPECTED_BUNDLE_HASH:-}"
FRESH_START_NEW_BUNDLE_HASH="${GSD_FRESH_START_NEW_BUNDLE_HASH:-}"
unset GSD_FRESH_START_REASON GSD_FRESH_START_EXPECTED_ROLE_CONFIG_HASH \
  GSD_FRESH_START_EXPECTED_BUNDLE_HASH GSD_FRESH_START_NEW_BUNDLE_HASH
RESUME_REQUESTED=0
FRESH_ROLE_PIN_RECOVERY_REQUESTED=0
FRESH_BUNDLE_RECOVERY_REQUESTED=0
if [ "$GSD_SKILL_NAME" = gsd-resume-work ]; then
  if [ "$GSD_RESUME_EXPLICIT" -eq 1 ] && [ "$GSD_RESUME" = 0 ]; then
    echo "gsd-run: gsd-resume-work cannot be used with explicit GSD_RESUME=0" >&2
    exit 2
  fi
  RESUME_REQUESTED=1
elif [ "$GSD_RESUME_EXPLICIT" -eq 1 ] && [ "$GSD_RESUME" = 0 ]; then
  # This is deliberately not a general resume-drift escape hatch. The
  # prelaunch tuple helper admits exactly one named recovery kind for an
  # explicitly named, failed, same-host run. Bundle repair requires both an
  # old and a new hash; role-pin repair retains its existing single-old-hash
  # contract. Mixing them is an operator-error, never a broader waiver.
  if [ -n "$FRESH_START_EXPECTED_ROLE_CONFIG_HASH" ] \
    && { [ -n "$FRESH_START_EXPECTED_BUNDLE_HASH" ] || [ -n "$FRESH_START_NEW_BUNDLE_HASH" ]; }; then
    echo "gsd-run: explicit fresh-start recovery kinds role_config_hash and bundle_hash are mutually exclusive" >&2
    exit 2
  elif [ -n "$FRESH_START_EXPECTED_BUNDLE_HASH" ] || [ -n "$FRESH_START_NEW_BUNDLE_HASH" ]; then
    FRESH_BUNDLE_RECOVERY_REQUESTED=1
  else
    FRESH_ROLE_PIN_RECOVERY_REQUESTED=1
  fi
elif [ "${GSD_RESUME:-0}" = 1 ]; then
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
if { [ "$FRESH_ROLE_PIN_RECOVERY_REQUESTED" -eq 1 ] || [ "$FRESH_BUNDLE_RECOVERY_REQUESTED" -eq 1 ]; } && [ -z "${GSD_RUN_ID:-}" ]; then
  echo "gsd-run: explicit fresh-start recovery requires an explicit GSD_RUN_ID" >&2
  exit 2
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
# GSD_EXTRA_WRITABLE_WORKTREE is deliberately narrower than a generic
# writable-roots escape hatch: it names one or more colon-delimited, existing
# sibling worktrees.  The validated physical paths are the only values that
# reach the Codex workspace-write configuration, and their stable list is
# resume-critical below.
declare -a EXTRA_WRITABLE_WORKTREES=()
EXTRA_WRITABLE_WORKTREES_TUPLE="none"
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

FRESH_START_RECOVERY_ARCHIVE_DIR=""
capture_explicit_fresh_start_recovery_state() {
  { [ "$FRESH_ROLE_PIN_RECOVERY_REQUESTED" -eq 1 ] || [ "$FRESH_BUNDLE_RECOVERY_REQUESTED" -eq 1 ]; } || return 0
  FRESH_START_RECOVERY_ARCHIVE_DIR="$(/usr/bin/python3 - "$RUN_STATE_DIR" "$RUN_TUPLE_FILE" "$RUN_STATUS_FILE" <<'PY'
import os
import stat
import sys
import tempfile

state_dir, tuple_path, status_path = sys.argv[1:]

def fail(message):
    print(f"gsd-run: explicit fresh-start recovery refused: {message}", file=sys.stderr)
    raise SystemExit(78)

def read_regular(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        fail("state artifacts must be readable regular non-symlink files")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            fail("state artifacts must be regular non-symlink files")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)

def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]

old_tuple_raw = read_regular(tuple_path)
old_status_raw = read_regular(status_path)
try:
    archive = tempfile.mkdtemp(prefix="gsd-run.archive.", dir=state_dir)
    os.chmod(archive, 0o700)
    for name, raw in (("tuple", old_tuple_raw), ("status", old_status_raw)):
        fd = os.open(os.path.join(archive, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
    dirfd = os.open(archive, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)
except OSError:
    fail("unable to snapshot failed state safely")
print(archive)
PY
)" || return 78
  [ -n "$FRESH_START_RECOVERY_ARCHIVE_DIR" ] || return 78
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
  capture_explicit_fresh_start_recovery_state || return $?
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

validate_extra_writable_worktrees() {
  local requested expected_parent expected_parent_real candidate candidate_real
  local actual_top actual_common registered registered_path registered_real
  local prior

  EXTRA_WRITABLE_WORKTREES=()
  EXTRA_WRITABLE_WORKTREES_TUPLE="none"
  requested="${GSD_EXTRA_WRITABLE_WORKTREE:-}"
  [ -n "$requested" ] || return 0

  # A colon-delimited absolute-path list keeps the capability explicit in an
  # environment variable while rejecting ambiguous empty elements.  This is
  # intentionally not a general path-list mechanism.
  case "$requested" in
    :*|*:|*::*|*$'\n'*|*$'\r'*)
      echo "gsd-run: GSD_EXTRA_WRITABLE_WORKTREE must be a colon-delimited list of non-empty absolute paths" >&2
      return 78
      ;;
  esac

  expected_parent="$PROJECT_PRIMARY_ROOT/.claude/worktrees"
  [ -d "$expected_parent" ] && [ ! -L "$expected_parent" ] || {
    echo "gsd-run: refusing invalid extra writable worktree parent" >&2
    return 78
  }
  expected_parent_real="$(cd "$expected_parent" && pwd -P)" || return 78
  [ "$expected_parent_real" = "$expected_parent" ] || {
    echo "gsd-run: extra writable worktree parent escapes the project worktrees namespace" >&2
    return 78
  }

  local IFS=:
  local -a candidates=()
  read -r -a candidates <<< "$requested"
  for candidate in "${candidates[@]}"; do
    case "$candidate" in
      /*) ;;
      *)
        echo "gsd-run: extra writable worktree must be an absolute path" >&2
        return 78
        ;;
    esac
    [ -d "$candidate" ] && [ ! -L "$candidate" ] || {
      echo "gsd-run: refusing nonexistent or symlinked extra writable worktree: $candidate" >&2
      return 78
    }
    candidate_real="$(cd "$candidate" && pwd -P)" || return 78
    case "$candidate_real" in
      "$expected_parent_real"/*) ;;
      *)
        echo "gsd-run: extra writable worktree escapes the project worktrees namespace" >&2
        return 78
        ;;
    esac
    [ "$candidate_real" != "$RUN_WORKTREE_ROOT" ] || {
      echo "gsd-run: extra writable worktree must be distinct from the active run worktree" >&2
      return 78
    }
    actual_top="$($GIT_BIN_FIXED -C "$candidate_real" rev-parse --show-toplevel 2>/dev/null || true)"
    [ -n "$actual_top" ] || {
      echo "gsd-run: extra writable path is not a registered Git worktree: $candidate" >&2
      return 78
    }
    actual_top="$(cd "$actual_top" && pwd -P)" || return 78
    [ "$actual_top" = "$candidate_real" ] || {
      echo "gsd-run: extra writable path must name a Git worktree root: $candidate" >&2
      return 78
    }
    actual_common="$($GIT_BIN_FIXED -C "$candidate_real" rev-parse --git-common-dir 2>/dev/null || true)"
    [ -n "$actual_common" ] || {
      echo "gsd-run: extra writable path is not a registered Git worktree: $candidate" >&2
      return 78
    }
    case "$actual_common" in
      /*) actual_common="$(cd "$actual_common" && pwd -P)" ;;
      *) actual_common="$(cd "$candidate_real/$actual_common" && pwd -P)" ;;
    esac
    [ "$actual_common" = "$GIT_COMMON_DIR" ] || {
      echo "gsd-run: extra writable worktree belongs to a different Git common directory" >&2
      return 78
    }
    registered=0
    while IFS= read -r registered_path; do
      case "$registered_path" in
        worktree\ *)
          registered_path="${registered_path#worktree }"
          registered_real="$(cd "$registered_path" 2>/dev/null && pwd -P)" || continue
          [ "$registered_real" != "$candidate_real" ] || { registered=1; break; }
          ;;
      esac
    done < <("$GIT_BIN_FIXED" -C "$REPO_ROOT" worktree list --porcelain)
    [ "$registered" -eq 1 ] || {
      echo "gsd-run: extra writable path is not a registered Git worktree: $candidate" >&2
      return 78
    }
    for prior in "${EXTRA_WRITABLE_WORKTREES[@]}"; do
      [ "$prior" != "$candidate_real" ] || {
        echo "gsd-run: duplicate extra writable worktree: $candidate" >&2
        return 78
      }
    done
    EXTRA_WRITABLE_WORKTREES+=("$candidate_real")
  done

  # Compact JSON is an injective representation of the exact canonical list:
  # unlike a separator join, a valid filesystem name cannot make one root
  # serialize as two. Reordering remains deliberate drift rather than a
  # silent capability substitution.
  EXTRA_WRITABLE_WORKTREES_TUPLE="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:], separators=(",", ":")))' "${EXTRA_WRITABLE_WORKTREES[@]}")" || return 1
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

# spec-006: a stale runner-worktree copy of .planning/phases/<slug> must never
# be reviewed/executed silently against a repo copy that has since diverged.
# Every branch below is an early `return 0` (silent, no behavior change)
# unless divergence is actually found. Gated by GSD_PLANNING_GUARD (kill
# switch) and, on divergence, GSD_PLANNING_SYNC picks a sync direction —
# unset/empty fails closed with exit 78.
#
# Set by check_planning_divergence when a sync REPLACED the repo-side phase
# directory. The plan wall (line ~133) reviewed that directory before this
# guard ever ran, so a repo-side replacement retires the wall's evidence.
PLANNING_SYNC_REPO_CHANGED=0

# Phase-directory basename owning phase number $2 under root $1 (empty when
# none). Mirrors the ^0*{N}- regex the ownership gate and plan wall use.
resolve_phase_slug() {
  python3 - "$1" "$2" <<'PY'
import re, sys
from pathlib import Path
root, phase_number = sys.argv[1], int(sys.argv[2], 10)
phases_root = Path(root) / ".planning" / "phases"
dirs = sorted(p for p in phases_root.glob("*-*") if p.is_dir() and re.match(rf"^0*{phase_number}-", p.name))
print(dirs[0].name if dirs else "", end="")
PY
}

# Copy $1 onto $2 without ever destroying $2 first: stage a sibling temp,
# stash the live destination, promote, and only then drop the stash. Any
# failure restores the stash and emits a typed line. $3=direction $4=slug.
planning_sync_copy() {
  local src="$1" dest="$2" direction="$3" slug="$4" tmp bak
  tmp="$dest.tmp.$$"
  bak="$dest.bak.$$"
  rm -rf "$tmp" "$bak"
  mkdir -p "$(dirname "$dest")" || {
    echo "GSD-RUN:PLANNING-SYNC-FAILED direction=$direction phase=$slug stage=parent" >&2
    return 1
  }
  if ! cp -R "$src" "$tmp"; then
    rm -rf "$tmp"
    echo "GSD-RUN:PLANNING-SYNC-FAILED direction=$direction phase=$slug stage=stage" >&2
    return 1
  fi
  if [ -e "$dest" ] && ! mv "$dest" "$bak"; then
    rm -rf "$tmp"
    echo "GSD-RUN:PLANNING-SYNC-FAILED direction=$direction phase=$slug stage=stash" >&2
    return 1
  fi
  if ! mv "$tmp" "$dest"; then
    [ -e "$bak" ] && mv "$bak" "$dest"
    rm -rf "$tmp"
    echo "GSD-RUN:PLANNING-SYNC-FAILED direction=$direction phase=$slug stage=promote" >&2
    return 1
  fi
  rm -rf "$bak"
  return 0
}

check_planning_divergence() {
  local phase_num slug repo_dir wt_dir repo_hash wt_hash newer guarded
  local repo_exists=0 wt_exists=0
  [ "${GSD_PLANNING_GUARD:-}" != off ] || return 0
  case "$GSD_SKILL_NAME" in
    gsd-execute-phase|gsd-plan-phase) ;;
    *) return 0 ;;
  esac
  phase_num="${2:-}"
  [[ "$phase_num" =~ ^[0-9]+$ ]] || return 0
  [ -d "$REPO_ROOT/.planning" ] && [ -d "$RUN_WORKTREE_ROOT/.planning" ] || return 0
  for guarded in "$REPO_ROOT/.planning" "$RUN_WORKTREE_ROOT/.planning"; do
    [ ! -L "$guarded" ] || {
      echo "gsd-run: PLANNING-GUARD refused: symlinked planning path: $guarded" >&2
      return 78
    }
  done
  slug="${GSD_PHASE_ID:-}"
  if [ -z "$slug" ]; then
    # Resolve from the repo first, then the worktree: a phase directory that
    # exists on only one side is exactly the one-sided divergence this guard
    # must catch, so an empty repo-side resolution is not "nothing to check".
    slug="$(resolve_phase_slug "$REPO_ROOT" "$phase_num")"
    [ -n "$slug" ] || slug="$(resolve_phase_slug "$RUN_WORKTREE_ROOT" "$phase_num")"
  fi
  [ -n "$slug" ] || return 0
  # The slug is interpolated into every path this function copies onto or
  # stashes. Validate BEFORE the first interpolation: a single path segment,
  # no traversal, fail closed on anything else.
  if ! [[ "$slug" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || [[ "$slug" == *..* ]]; then
    echo "gsd-run: PLANNING-GUARD refused: invalid phase slug" >&2
    return 78
  fi
  repo_dir="$REPO_ROOT/.planning/phases/$slug"
  wt_dir="$RUN_WORKTREE_ROOT/.planning/phases/$slug"
  for guarded in "$repo_dir" "$wt_dir"; do
    [ ! -L "$guarded" ] || {
      echo "gsd-run: PLANNING-GUARD refused: symlinked planning path: $guarded" >&2
      return 78
    }
  done
  [ -d "$repo_dir" ] && repo_exists=1
  [ -d "$wt_dir" ] && wt_exists=1
  if [ "$repo_exists" -eq 0 ] && [ "$wt_exists" -eq 0 ]; then
    return 0
  elif [ "$repo_exists" -eq 1 ] && [ "$wt_exists" -eq 1 ]; then
    repo_hash="$(sha256_tree "$repo_dir")" || {
      echo "gsd-run: PLANNING-DIVERGENCE check failed for phase $slug" >&2
      return 1
    }
    wt_hash="$(sha256_tree "$wt_dir")" || {
      echo "gsd-run: PLANNING-DIVERGENCE check failed for phase $slug" >&2
      return 1
    }
    [ "$repo_hash" = "$wt_hash" ] && return 0
    # Determine which side is newer: for every relative path where content
    # differs or the path exists on only one side, attribute that path's mtime
    # to whichever side(s) hold it. Ties resolve to repo (deterministic).
    # Advisory only — a failure here degrades to newer=unknown and never
    # relaxes the fail-closed decision below.
    newer="$(python3 - "$repo_dir" "$wt_dir" <<'PY'
import hashlib, sys
from pathlib import Path

CHUNK = 1 << 20

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            h.update(block)
    return h.digest()

def walk(root):
    return {
        p.relative_to(root).as_posix(): p
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }

repo_root, wt_root = Path(sys.argv[1]), Path(sys.argv[2])
repo_files, wt_files = walk(repo_root), walk(wt_root)
repo_max = wt_max = 0.0
for rel in set(repo_files) | set(wt_files):
    rp, wp = repo_files.get(rel), wt_files.get(rel)
    if rp is not None and wp is not None:
        rs, ws = rp.stat(), wp.stat()
        if rs.st_size == ws.st_size and digest(rp) == digest(wp):
            continue
        if rs.st_mtime >= ws.st_mtime:
            repo_max = max(repo_max, rs.st_mtime)
        else:
            wt_max = max(wt_max, ws.st_mtime)
    elif rp is not None:
        repo_max = max(repo_max, rp.stat().st_mtime)
    else:
        wt_max = max(wt_max, wp.stat().st_mtime)
print("repo" if repo_max >= wt_max else "worktree", end="")
PY
)" || newer=""
    [ -n "$newer" ] || newer=unknown
  elif [ "$repo_exists" -eq 1 ]; then
    # One-sided existence is divergence, not agreement: the missing side was
    # never reviewed against the side that has content. Fail closed.
    newer=repo
  else
    newer=worktree
  fi
  case "${GSD_PLANNING_SYNC:-}" in
    '')
      echo "GSD-RUN:PLANNING-DIVERGENCE phase=$slug newer=$newer" >&2
      return 78
      ;;
    repo)
      [ "$repo_exists" -eq 1 ] || {
        echo "gsd-run: cannot sync .planning direction=repo phase=$slug: source missing" >&2
        return 1
      }
      planning_sync_copy "$repo_dir" "$wt_dir" repo "$slug" || return 1
      echo "GSD-RUN:PLANNING-SYNC direction=repo phase=$slug" >&2
      return 0
      ;;
    worktree)
      [ "$wt_exists" -eq 1 ] || {
        echo "gsd-run: cannot sync .planning direction=worktree phase=$slug: source missing" >&2
        return 1
      }
      planning_sync_copy "$wt_dir" "$repo_dir" worktree "$slug" || return 1
      PLANNING_SYNC_REPO_CHANGED=1
      echo "GSD-RUN:PLANNING-SYNC direction=worktree phase=$slug" >&2
      return 0
      ;;
    *)
      echo "gsd-run: invalid GSD_PLANNING_SYNC value: ${GSD_PLANNING_SYNC:-}" >&2
      return 78
      ;;
  esac
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
  case "$major:$minor:$patch" in *[!0-9:]*|::*|*::|*:) return 1 ;; esac
  { [ "$major" -eq 0 ] && [ "$minor" -ge 137 ] && [ "$minor" -lt 148 ]; } \
    || { [ "$major" -eq 0 ] && [ "$minor" -eq 154 ] && [ "$patch" -eq 0 ]; } \
    || { [ "$major" -eq 0 ] && [ "$minor" -eq 155 ] && [ "$patch" -eq 1 ]; } \
    || { [ "$major" -eq 0 ] && [ "$minor" -eq 156 ] && [ "$patch" -eq 1 ]; } \
    || { [ "$major" -eq 0 ] && [ "$minor" -eq 157 ] && [ "$patch" -eq 0 ]; }
}

require_supported_codex_cli() {
  local bin="$1" raw version
  [ -n "$CODEX_CLI_VERSION" ] && return 0
  raw="$("$bin" --version 2>/dev/null)" || {
    echo "gsd-run: could not determine Codex CLI version" >&2
    return 78
  }
  version="$(printf '%s\n' "$raw" | sed -nE 's/^[[:space:]]*[^[:space:]]+[[:space:]]+([0-9]+\.[0-9]+\.[0-9]+)[[:space:]]*$/\1/p' | head -1)"
  if [ -z "$version" ] || ! version_in_supported_codex_range "$version"; then
    CODEX_PREFLIGHT_FATAL=1
    echo "gsd-run: Codex CLI ${version:-unknown} is outside supported range >=0.137.0,<0.148.0 or exact 0.154.0 / 0.155.1 / 0.156.1 / 0.157.0" >&2
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

ensure_git_ref_parent() {
  local anchor="$1" ref_tail="$2" current component current_real saved_ifs
  [ -d "$anchor" ] && [ ! -L "$anchor" ] || {
    echo "gsd-run: refusing symlinked linked-worktree branch-ref parent" >&2
    return 78
  }
  current="$(cd "$anchor" && pwd -P)" || return 78
  [ "$current" = "$anchor" ] || {
    echo "gsd-run: linked-worktree branch-ref parent escapes Git common directory" >&2
    return 78
  }
  saved_ifs="$IFS"
  IFS=/
  set -- $ref_tail
  IFS="$saved_ifs"
  # The final component is the ref file; each preceding component is a
  # directory Git may need to create before it can atomically write .lock.
  while [ "$#" -gt 1 ]; do
    component="$1"
    shift
    current="$current/$component"
    if [ -e "$current" ]; then
      [ -d "$current" ] && [ ! -L "$current" ] || {
        echo "gsd-run: refusing symlinked linked-worktree branch-ref parent" >&2
        return 78
      }
    else
      mkdir "$current" || return 1
    fi
    current_real="$(cd "$current" && pwd -P)" || return 78
    [ "$current_real" = "$current" ] || {
      echo "gsd-run: linked-worktree branch-ref parent escapes Git common directory" >&2
      return 78
    }
  done
}

prepare_codex_runtime() {
  local source_root skill_root auth_source network_bool writable_json trusted_package node_bin auth_meta auth_uid auth_mode
  local admin_dir admin_parent branch_ref branch_ref_rc branch_tail branch_ref_file branch_ref_lock branch_log_file branch_log_lock
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
  # Git worktree move preserves the admin directory basename, so it is NOT
  # derivable from RUN_ID. Ask trusted Git for the exact admin directory and
  # require its physical parent to be <common>/worktrees before granting it.
  admin_dir="$($GIT_BIN_FIXED -C "$RUN_WORKTREE_ROOT" rev-parse --absolute-git-dir 2>/dev/null)" || return 78
  [ -d "$admin_dir" ] && [ ! -L "$admin_dir" ] || {
    echo "gsd-run: refusing invalid linked-worktree admin directory" >&2; return 78; }
  admin_dir="$(cd "$admin_dir" && pwd -P)" || return 78
  admin_parent="$(dirname "$admin_dir")"
  if [ "$admin_parent" != "$GIT_COMMON_DIR/worktrees" ]; then
    echo "gsd-run: linked-worktree admin directory escapes the Git common worktrees namespace" >&2
    return 78
  fi
  # A normal commit atomically writes its exact branch ref and reflog through
  # sibling .lock files. Grant those four paths only — never refs/heads,
  # another branch namespace, or the whole Git common directory.
  branch_ref="$($GIT_BIN_FIXED -C "$RUN_WORKTREE_ROOT" symbolic-ref -q HEAD 2>/dev/null)"
  branch_ref_rc=$?
  case "$branch_ref_rc" in
    0|1) ;;
    *) echo "gsd-run: unable to resolve linked-worktree symbolic ref" >&2; return 78 ;;
  esac
  case "$branch_ref" in
    "") ;;
    refs/heads/*)
      $GIT_BIN_FIXED check-ref-format "$branch_ref" >/dev/null 2>&1 || {
        echo "gsd-run: refusing invalid linked-worktree branch ref" >&2; return 78; }
      branch_ref_file="$GIT_COMMON_DIR/$branch_ref"
      branch_ref_lock="$branch_ref_file.lock"
      branch_log_file="$GIT_COMMON_DIR/logs/$branch_ref"
      branch_log_lock="$branch_log_file.lock"
      # Validate both fixed anchors before creating a nested branch parent:
      # mkdir -p follows symlinked parents, so checking only the final ref
      # file would otherwise permit a linked worktree to escape .git.
      branch_tail="$(printf '%s' "$branch_ref" | sed 's#^refs/heads/##')"
      ensure_git_ref_parent "$GIT_COMMON_DIR/refs/heads" "$branch_tail" || return $?
      ensure_git_ref_parent "$GIT_COMMON_DIR/logs/refs/heads" "$branch_tail" || return $?
      for _git_ref_path in "$branch_ref_file" "$branch_ref_lock" "$branch_log_file" "$branch_log_lock"; do
        [ ! -L "$_git_ref_path" ] || {
          echo "gsd-run: refusing symlinked linked-worktree branch artifact" >&2; return 78; }
      done
      ;;
    *) echo "gsd-run: refusing non-head linked-worktree symbolic ref" >&2; return 78 ;;
  esac
  writable_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
    "$RUN_WORKTREE_ROOT" "$PROJECT_PRIMARY_ROOT/.feature-fix-swarm" \
    "$GIT_COMMON_DIR/objects" "$admin_dir" "${EXTRA_WRITABLE_WORKTREES[@]}" \
    ${branch_ref_file:+"$branch_ref_file" "$branch_ref_lock" "$branch_log_file" "$branch_log_lock"})" || return 1
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

archive_explicit_fresh_start_recovery() {
  local new_tuple="$1"
  local kind
  if [ "$FRESH_BUNDLE_RECOVERY_REQUESTED" -eq 1 ]; then kind=bundle_hash; else kind=role_config_hash; fi
  [ -n "$FRESH_START_RECOVERY_ARCHIVE_DIR" ] || {
    echo "gsd-run: explicit fresh-start recovery refused: failed-state snapshot is missing" >&2
    return 78
  }
  /usr/bin/python3 - "$FRESH_START_RECOVERY_ARCHIVE_DIR" "$new_tuple" "$RUN_ID" "$GSD_SKILL_NAME" "$SELECTED_HOST" \
    "$kind" "$FRESH_START_EXPECTED_ROLE_CONFIG_HASH" "$FRESH_START_EXPECTED_BUNDLE_HASH" \
    "$FRESH_START_NEW_BUNDLE_HASH" "$FRESH_START_REASON" <<'PY'
import hashlib
import os
import re
import stat
import sys

archive, new_path, run_id, skill, host, kind, expected_role, expected_bundle, new_bundle, reason = sys.argv[1:]

def fail(message):
    print(f"gsd-run: explicit fresh-start recovery refused: {message}", file=sys.stderr)
    raise SystemExit(78)

def read_regular(name, dirfd=None):
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dirfd)
    except OSError:
        fail("recovery artifact is unreadable")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            fail("recovery artifact is not regular")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)

def parse(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("tuple data is malformed")
    out = {}
    for line in text.splitlines():
        if not line or "=" not in line:
            fail("tuple data is malformed")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[a-z_]+", key) or key in out:
            fail("tuple data is malformed")
        out[key] = value
    return out

def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]

try:
    dirfd = os.open(archive, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
except OSError:
    fail("failed-state archive is unavailable")
try:
    old_tuple_raw = read_regular("tuple", dirfd)
    old_status_raw = read_regular("status", dirfd)
    new_raw = read_regular(new_path)
    old_tuple, old_status, new_tuple = map(parse, (old_tuple_raw, old_status_raw, new_raw))
    if old_tuple.get("run_id") != run_id or old_tuple.get("skill") != skill:
        fail("prior tuple is not this explicit run and skill")
    if old_status.get("state") != "failed" or old_status.get("skill") != skill:
        fail("prior status is not a failed drive for this skill")
    if old_status.get("host") != host or old_tuple.get("runtime") != host:
        fail("prior drive host differs from selected host")
    old_cmp, new_cmp = dict(old_tuple), dict(new_tuple)
    old_cmp.pop("auth_initial_hash", None)
    new_cmp.pop("auth_initial_hash", None)
    drift = {k for k in set(old_cmp) | set(new_cmp) if old_cmp.get(k) != new_cmp.get(k)}
    if kind == "role_config_hash":
        old_role, new_role = old_tuple.get("role_config_hash", ""), new_tuple.get("role_config_hash", "")
        if old_role != expected_role or not re.fullmatch(r"[0-9a-f]{64}", old_role):
            fail("expected old role hash does not match the prior tuple")
        if not re.fullmatch(r"[0-9a-f]{64}", new_role) or old_role == new_role:
            fail("role hash must be the sole actual change")
        if drift != {"role_config_hash"}:
            fail("only role_config_hash may change")
        recovery_metadata = (
            "schema=ffs.gsd-run-role-pin-recovery/v1\noutcome=accepted\n"
            "recovery_kind=role_config_hash\n"
            f"old_role_config_hash={old_role}\nnew_role_config_hash={new_role}\n"
        )
    elif kind == "bundle_hash":
        old_bundle, actual_new_bundle = old_tuple.get("bundle_hash", ""), new_tuple.get("bundle_hash", "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_bundle) or not re.fullmatch(r"[0-9a-f]{64}", new_bundle):
            fail("bundle recovery requires exact expected old and new bundle hashes")
        if old_bundle != expected_bundle:
            fail("expected old bundle hash does not match the prior tuple")
        if actual_new_bundle != new_bundle:
            fail("expected new bundle hash does not match the candidate tuple")
        if old_bundle == actual_new_bundle:
            fail("bundle hash must change during bundle recovery")
        if drift != {"bundle_hash"}:
            fail("only bundle_hash may change")
        recovery_metadata = (
            "schema=ffs.gsd-run-bundle-recovery/v1\noutcome=accepted\n"
            "recovery_kind=bundle_hash\n"
            f"old_bundle_hash={old_bundle}\nnew_bundle_hash={actual_new_bundle}\n"
        )
    else:
        fail("unknown fresh-start recovery kind")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,160}", reason):
        fail("reason must be a single-line audit label")
    metadata = (
        recovery_metadata
        + f"reason={reason}\nrun_id={run_id}\nskill={skill}\nhost={host}\n"
        f"old_tuple_sha256={hashlib.sha256(old_tuple_raw).hexdigest()}\n"
        f"old_status_sha256={hashlib.sha256(old_status_raw).hexdigest()}\n"
        f"new_tuple_sha256={hashlib.sha256(new_raw).hexdigest()}\n"
        "archived_at=" + __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n"
    ).encode()
    fd = os.open("metadata.next", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dirfd)
    try:
        write_all(fd, metadata)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace("metadata.next", "metadata", src_dir_fd=dirfd, dst_dir_fd=dirfd)
    os.fsync(dirfd)
finally:
    os.close(dirfd)
PY
}

normalize_prelaunch_tuple_for_compare() {
  local tuple="$1"
  # Older persisted tuples predate the extra-worktree capability.  Treat an
  # absent field as the safe default while retaining strict comparison for
  # every present capability and every other resume-critical field.  Moving
  # this field to a stable final position makes the one-field compatibility
  # normalization independent of tuple line ordering.
  awk '
    /^auth_initial_hash=/ { next }
    /^extra_writable_worktrees=/ {
      seen += 1
      if (seen == 1) {
        # The first implementation used the safe literal `none`; normalize
        # it to the JSON empty-list form without accepting any ambiguous
        # prior non-empty separator encoding.
        if ($0 == "extra_writable_worktrees=none") extra = "extra_writable_worktrees=[]"
        else extra = $0
      }
      else print
      next
    }
    { print }
    END {
      if (seen == 0) print "extra_writable_worktrees=[]"
      else print extra
    }
  ' "$tuple"
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
    printf 'extra_writable_worktrees=%s\n' "$EXTRA_WRITABLE_WORKTREES_TUPLE"
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
      <(normalize_prelaunch_tuple_for_compare "$RUN_TUPLE_FILE") \
      <(normalize_prelaunch_tuple_for_compare "$tmp") >/dev/null; then
      echo "gsd-run: resume tuple drift; refusing to launch with changed runtime/model/CLI/skill/sandbox" >&2
      diff -u "$RUN_TUPLE_FILE" "$tmp" >&2 || true
      rm -f "$tmp"
      return 78
    fi
    rm -f "$tmp"
  elif [ "$FRESH_ROLE_PIN_RECOVERY_REQUESTED" -eq 1 ] || [ "$FRESH_BUNDLE_RECOVERY_REQUESTED" -eq 1 ]; then
    # Explicit GSD_RESUME=0 can replace a failed tuple only through the
    # narrow role-pin or exact bundle archival path above. It rejects missing
    # or incomplete state; ordinary first launches leave GSD_RESUME unset.
    archive_explicit_fresh_start_recovery "$tmp" || {
      rm -f "$tmp"
      return 78
    }
    atomic_replace "$tmp" "$RUN_TUPLE_FILE"
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
      output="$(run_bounded "$PROBE_TIMEOUT_SECS" env -u OPENAI_API_KEY -u GSD_EXTRA_WRITABLE_WORKTREE "$bin" exec \
        -c "model=\"$model\"" -c "model_reasoning_effort=\"$effort\"" \
        --sandbox read-only --ephemeral --ignore-user-config --ignore-rules \
        --color never "$PROBE_PROMPT" </dev/null 2>&1)"
      rc=$?
    else
      output="$(run_bounded "$PROBE_TIMEOUT_SECS" env \
        -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u GSD_EXTRA_WRITABLE_WORKTREE \
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
validate_extra_writable_worktrees || exit $?
# The runner has consumed and pinned this input.  Never propagate a mutable
# sandbox capability request into the stateful agent environment.
unset GSD_EXTRA_WRITABLE_WORKTREE
check_planning_divergence "$@" || exit $?
# The plan wall above (pre-execution seam) reviewed the REPO phase directory.
# A worktree-direction sync just replaced that reviewed content with content
# no wall has seen, which would launch the executor against unreviewed plans.
# Re-run the same lever against the synced directory. The repo direction needs
# no re-run: the repo copy is the one the wall already cleared, and
# gsd-plan-phase has no wall to retire.
if [ "$PLANNING_SYNC_REPO_CHANGED" -eq 1 ] && [ "$GSD_SKILL_NAME" = gsd-execute-phase ]; then
  _gsd_run_wall_gate "$WALL_PHASE_DIR"
  _resync_wall_rc=$?
  if [ "$_resync_wall_rc" -ne 0 ]; then
    echo "GSD-RUN:PLANNING-SYNC-WALL-FAILED phase=$GSD_PHASE_ID rc=$_resync_wall_rc" >&2
    exit "$_resync_wall_rc"
  fi
fi
budget_prepare_mapping || exit $?

# Original invocation, preserved for session-wake resume records: the
# reconciler re-runs this exact runner argv when the wake condition fires.
GSD_ORIG_ARGV=("scripts/gsd/gsd-run.sh" "$@")
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
  CODEX_COMMAND="$CODEX_COMMAND

FFS CODEX STAGED-WORKFLOW CONTRACT: Before doing any work, fully read the exact verified staged workflow at \"$CODEX_RUNTIME_HOME/skills/$GSD_SKILL_NAME/SKILL.md\" under CODEX_HOME. Follow every linked workflow it directs you to. Do not use any legacy/local feature-implement skill or infer the workflow from the slash-command name."
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
  # Session-limit banner in a failed drive: checkpoint a durable waiting(time)
  # record and yield — the reconciler relaunches at the reset time. Checked
  # BEFORE the capture is deleted and before any respawn decision (AC-005).
  if [ "$rc" -ne 0 ] && [ "${FFS_SESSION_WAKE:-on}" != off ] && [ -x "$SCRIPT_DIR/session-wake.sh" ]; then
    if wake_out="$(bash "$SCRIPT_DIR/session-wake.sh" checkpoint "$DRIVE_CAPTURE" "$rc" \
        --run-id "$RUN_ID" --resume-argv "${GSD_ORIG_ARGV[@]}" 2>&1)" \
       && [[ "$wake_out" == *SESSION-WAKE:wake-at:* ]]; then
      printf '%s\n' "$wake_out" >> "$LOG_FILE"
      echo "GSD-RUN:SESSION-WAKE checkpointed run=$RUN_ID rc=$rc — resume deferred to reconcile" >&2
      printf 'GSD-RUN:SESSION-WAKE checkpointed run=%s rc=%s\n' "$RUN_ID" "$rc" >> "$LOG_FILE"
      rm -f "$DRIVE_CAPTURE"
      break
    fi
  fi
  rm -f "$DRIVE_CAPTURE"
  [ "$rc" -eq 0 ] && break
  [ "$attempt" -le "$RESPAWN_MAX" ] || break
  grep -qx 'state=quarantined' "$RUN_STATUS_FILE" 2>/dev/null && break
  # A coord claim abort (CLAIM-SUPERSEDED / CLAIM-STALE) is a deliberate
  # kill — another session owns the run now. Respawning would race the new
  # owner; propagate the abort rc instead.
  [ ! -f "$RUN_STATE_DIR/gsd-run.coord-abort" ] || break
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
