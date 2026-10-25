#!/usr/bin/env bash
# drive.sh -- FFS-owned detached supervisor for multi-step autonomous drives.
#
# Replaces hand-written "driver" loops around gsd-run.sh (spec-443's
# plan-phases-driver.sh died with the Claude Code session that spawned it, and
# its operator rule "stop after two failed attempts" ended the run). This
# supervisor:
#
#   * runs in its OWN session/process group (python os.setsid: macOS has no
#     setsid binary), so a Claude Code background-task memory reap or a
#     session end cannot take it down;
#   * owns the per-step budgets -- step failures, environment deaths and
#     user-input blocks are three different things and are counted apart;
#   * reuses ONE gsd run id per step across attempts and passes GSD_RESUME=1,
#     so a retry resumes from .planning state instead of starting over;
#   * writes a lifecycle record for itself, so reconcile.sh relaunches it
#     (`drive.sh resume --run ID`) after the supervisor itself is killed;
#   * never fully stops: a blocked or quarantined step records a pending
#     action and the drive moves on (--on-blocked next) or parks as
#     waiting(manual) (--on-blocked park). Parked is a state, not an exit.
#
# Usage:
#   drive.sh start  --verb plan-phase|execute-phase --phases A-B [--project NNN]
#                   [--after-step CMD] [--on-blocked next|park] [--detach] [--run ID]
#   drive.sh resume --run ID          # stored resume argv; reconcile.sh calls this
#   drive.sh status [--run ID]        # DRIVE:alive|dead|done|parked ...
#   drive.sh stop   --run ID          # operator only -> failed operator-stop
#
# Budgets (env, all per step):
#   FFS_STEP_ATTEMPTS=5       step-failure attempts (rc other than the classes below)
#   FFS_ENV_DEATH_MAX=8       signal deaths (rc 129/137/143) + supervisor deaths;
#                             backoff FFS_ENV_BACKOFF_BASE_SECS(60)*2^k capped
#                             FFS_ENV_BACKOFF_CAP_SECS(900); never a step failure
#   FFS_NO_PROGRESS_MAX=3     consecutive failures with no new commit and the same
#                             fingerprint -> quarantine (runaway guard)
#   FFS_STEP_LAUNCH_CEILING=12  hard cap on launches of one step, all classes
#   FFS_WALL_WAIT_MAX=24      rc 75/76 (plan wall pending) waits, FFS_WALL_DEFER_SECS(900) each
#   FFS_STEP_BACKOFF_SECS=30  pause between step-failure attempts
#   FFS_DISK_MIN_MB=700       refuse to launch below this free disk; wait instead
#
# Signals on stdout/log, one decision per line: DRIVE:<event> key=value ...
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
STATE_DIR="$ROOT/.planning/run-state"
LIFE="$SCRIPT_DIR/lifecycle.sh"
GSD_RUN="${FFS_DRIVE_GSD_RUN:-$SCRIPT_DIR/gsd-run.sh}"
PRESSURE="$SCRIPT_DIR/host-pressure.sh"

STEP_ATTEMPTS="${FFS_STEP_ATTEMPTS:-5}"
ENV_DEATH_MAX="${FFS_ENV_DEATH_MAX:-8}"
ENV_BACKOFF_BASE="${FFS_ENV_BACKOFF_BASE_SECS:-60}"
ENV_BACKOFF_CAP="${FFS_ENV_BACKOFF_CAP_SECS:-900}"
NO_PROGRESS_MAX="${FFS_NO_PROGRESS_MAX:-3}"
LAUNCH_CEILING="${FFS_STEP_LAUNCH_CEILING:-12}"
WALL_WAIT_MAX="${FFS_WALL_WAIT_MAX:-24}"
WALL_DEFER_SECS="${FFS_WALL_DEFER_SECS:-900}"
STEP_BACKOFF="${FFS_STEP_BACKOFF_SECS:-30}"
DISK_MIN_MB="${FFS_DISK_MIN_MB:-700}"
DISK_WAIT_SECS="${FFS_DISK_WAIT_SECS:-300}"
DISK_MAX_WAIT_SECS="${FFS_DISK_MAX_WAIT_SECS:-7200}"

usage() { sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed -n 's/^# \{0,1\}//p' | sed -n '/^Usage:/,/^Budgets/p' | sed '$d'; }
fail() { printf 'DRIVE:%s\n' "$*" >&2; exit 1; }
say() { printf 'DRIVE:%s\n' "$*"; }
num() { [[ "${1:-}" =~ ^[0-9]+$ ]]; }
for v in STEP_ATTEMPTS ENV_DEATH_MAX ENV_BACKOFF_BASE ENV_BACKOFF_CAP NO_PROGRESS_MAX LAUNCH_CEILING WALL_WAIT_MAX WALL_DEFER_SECS STEP_BACKOFF DISK_MIN_MB DISK_WAIT_SECS DISK_MAX_WAIT_SECS; do
  num "${!v}" || fail "invalid-env $v=${!v}"
done

safe_id() { [[ "${1:-}" =~ ^[A-Za-z0-9_.-]+$ ]] && [ "${#1}" -le 100 ]; }
state_file() { printf '%s/drive-%s.json\n' "$STATE_DIR" "$1"; }
life() { (cd "$ROOT" && bash "$LIFE" "$@"); }

# dstate <file> key=value ...   atomic JSON mutation; values parse as JSON when
# they can (numbers, true/false, quoted strings), else as raw strings. Dotted
# keys descend into objects/lists. Always refreshes .heartbeat.
dstate() {
  python3 - "$@" <<'PY'
import json, os, sys, time
path, ops = sys.argv[1], sys.argv[2:]
with open(path) as fh:
    d = json.load(fh)
d["heartbeat"] = int(time.time())
for op in ops:
    key, _, raw = op.partition("=")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    cur = d
    parts = key.split(".")
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur.setdefault(part, {})
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value
tmp = f"{path}.tmp"
with open(tmp, "w") as fh:
    json.dump(d, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.replace(tmp, path)
PY
}
q() { jq -r "$1" "$STATE_FILE"; }

detach_exec() {
  # $@ = command; returns child pid on stdout. Own session + pgid; stdio off
  # the caller's terminal so the caller's exit cannot SIGHUP/SIGPIPE it.
  local log="$1"; shift
  nohup python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@" </dev/null >>"$log" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  printf '%s\n' "$pid"
}

free_disk_mb() { df -m "$ROOT" 2>/dev/null | awk 'NR==2{print $4}'; }

wait_for_disk() {
  local waited=0 free
  while :; do
    free="$(free_disk_mb)"
    num "$free" || return 0            # unreadable -> fail open
    [ "$free" -lt "$DISK_MIN_MB" ] || return 0
    if [ "$waited" -ge "$DISK_MAX_WAIT_SECS" ]; then
      say "disk-wait-exhausted free_mb=$free min_mb=$DISK_MIN_MB -- launching anyway"; return 0
    fi
    say "disk-low free_mb=$free min_mb=$DISK_MIN_MB sleep=${DISK_WAIT_SECS}s"
    dstate "$STATE_FILE" >/dev/null
    sleep "$DISK_WAIT_SECS"; waited=$((waited + DISK_WAIT_SECS))
  done
}

# Match gsd-run's exact common-dir layout, including consumer worktrees.
step_worktree() {
  local common primary wt
  common="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null)" || { printf '%s\n' "$ROOT"; return; }
  case "$common" in /*) ;; *) common="$ROOT/$common" ;; esac
  common="$(cd "$common" && pwd -P)" || return 1
  primary="$(dirname "$common")"
  wt="$primary/.claude/worktrees/$1"
  if [ -d "$wt" ]; then printf '%s\n' "$wt"; else printf '%s\n' "$ROOT"; fi
}

progress_stamp() {
  git -C "$(step_worktree "$1")" rev-parse HEAD 2>/dev/null || echo none
}

fingerprint() {
  # rc + last non-blank line of the step log, hashed: identical -> same failure
  local rc="$1" log="$2" line
  line="$(tail -n 60 "$log" 2>/dev/null | grep -v '^[[:space:]]*$' | tail -n 1)"
  printf '%s\n%s\n' "$rc" "$line" | cksum | awk '{print $1}'
}

record_pending() {
  # $1 phase $2 class $3 reason $4 rc $5 log
  local pending="$STATE_DIR/drive-$RUN.pending.md"
  [ -f "$pending" ] || printf '# Pending actions for drive %s\n\n' "$RUN" > "$pending"
  printf -- '- %s phase %s: %s (%s) rc=%s log=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "$4" "$5" >> "$pending"
}

CHILD_PID=""
on_term() {
  # Supervisor asked to stop (operator `stop`, or a kill of our own pgid).
  # Leave the lifecycle record `running`: if this was not an operator stop,
  # reconcile's dead-pid check relaunches us and the interrupted step is
  # charged as an environment death on resume.
  [ -z "$CHILD_PID" ] || kill -TERM "$CHILD_PID" 2>/dev/null || true
  say "terminated run=$RUN"
  exit 143
}

run_loop() {
  trap on_term TERM INT HUP
  local total idx phase step_run step_state attempts env_deaths launches no_progress wall_waits fp_prev
  local rc stamp_before stamp_after fp log resume on_blocked after_step verb reason
  total="$(q '.steps | length')"; verb="$(q .verb)"; on_blocked="$(q .on_blocked)"; after_step="$(q '.after_step // empty')"
  # Steps left `running` by a previous supervisor incarnation were interrupted
  # by its death -> environment death for that step, then retried.
  local i
  for i in $(seq 0 $((total - 1))); do
    if [ "$(q ".steps[$i].state")" = running ]; then
      dstate "$STATE_FILE" "steps.$i.state=\"pending\"" "steps.$i.env_deaths=$(( $(q ".steps[$i].env_deaths") + 1 ))" >/dev/null
      say "supervisor-death-charged phase=$(q ".steps[$i].phase") env_deaths=$(q ".steps[$i].env_deaths")"
    fi
  done
  while :; do
    idx="$(q .index)"
    if [ "$idx" -ge "$total" ]; then
      dstate "$STATE_FILE" 'state="done"' >/dev/null
      life transition "$RUN" "done" completed >/dev/null 2>&1 || true
      say "done run=$RUN steps=$total"; return 0
    fi
    step_state="$(q ".steps[$idx].state")"
    case "$step_state" in
      done|blocked|quarantined) dstate "$STATE_FILE" "index=$((idx + 1))" >/dev/null; continue ;;
    esac
    phase="$(q ".steps[$idx].phase")"; step_run="$(q ".steps[$idx].run_id")"
    attempts="$(q ".steps[$idx].attempts")"; env_deaths="$(q ".steps[$idx].env_deaths")"
    launches="$(q ".steps[$idx].launches")"; no_progress="$(q ".steps[$idx].no_progress")"
    wall_waits="$(q ".steps[$idx].wall_waits")"; fp_prev="$(q ".steps[$idx].fingerprint // empty")"
    log="$STATE_DIR/drive-$RUN-p$phase.log"

    if [ "$launches" -ge "$LAUNCH_CEILING" ]; then
      reason="launch-ceiling launches=$launches max=$LAUNCH_CEILING"
      quarantine_step "$idx" "$phase" "$reason" "$log"; continue
    fi
    wait_for_disk
    [ ! -f "$PRESSURE" ] || bash "$PRESSURE" wait >/dev/null 2>&1 || true
    resume=()
    [ "$attempts" -eq 0 ] && [ "$env_deaths" -eq 0 ] && [ "$wall_waits" -eq 0 ] || resume=(GSD_RESUME=1)
    stamp_before="$(progress_stamp "$step_run")"
    dstate "$STATE_FILE" "steps.$idx.state=\"running\"" "steps.$idx.launches=$((launches + 1))" "steps.$idx.started_at=$(date +%s)" >/dev/null
    say "launch phase=$phase run=$step_run attempt=$((attempts + 1))/$STEP_ATTEMPTS env_deaths=$env_deaths/$ENV_DEATH_MAX launches=$((launches + 1))/$LAUNCH_CEILING resume=${resume[*]:-fresh}"
    printf '\n==== DRIVE launch %s phase=%s attempt=%s ====\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$phase" "$((attempts + 1))" >> "$log"
    # FFS_LIFECYCLE_LAUNCH=off: this supervisor's record covers the tree.
    # FFS_RESPAWN_MAX defaults to 0 here: one owner of the attempt budget.
    # -u GSD_RESUME: an inherited GSD_RESUME (reconcile relaunches us with
    # GSD_RESUME=1) must never reach a FRESH step -- gsd-run treats an explicit
    # GSD_RESUME=0 as a fresh-start recovery request, so unset is the only
    # safe "fresh". Only attempts>1 / env-death / wall retries pass GSD_RESUME=1.
    env -u GSD_RESUME GSD_RUN_ID="$step_run" FFS_LIFECYCLE_LAUNCH=off FFS_RESPAWN_MAX="${FFS_DRIVE_INNER_RESPAWN_MAX:-0}" \
      ${resume[@]+"${resume[@]}"} bash "$GSD_RUN" "/gsd-$verb" "$phase" --auto >>"$log" 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID"; rc=$?; CHILD_PID=""
    launches=$((launches + 1))
    stamp_after="$(progress_stamp "$step_run")"
    dstate "$STATE_FILE" "steps.$idx.last_rc=$rc" "steps.$idx.head=\"$stamp_after\"" >/dev/null

    case "$rc" in
      0)
        if [ -n "$after_step" ]; then
          (cd "$(step_worktree "$step_run")" && bash -c "${after_step//\{N\}/$phase}") >>"$log" 2>&1
          rc=$?
          if [ "$rc" -ne 0 ]; then
            dstate "$STATE_FILE" "steps.$idx.last_rc=$rc" >/dev/null
            say "after-step-failed phase=$phase rc=$rc"
            step_failure "$idx" "$phase" "$rc" "$log" "$stamp_before" "$stamp_after" "$fp_prev"; continue
          fi
        fi
        dstate "$STATE_FILE" "steps.$idx.state=\"done\"" "index=$((idx + 1))" >/dev/null
        say "step-done phase=$phase run=$step_run attempts=$((attempts + 1)) env_deaths=$env_deaths"
        ;;
      129|137|143)
        env_deaths=$((env_deaths + 1))
        dstate "$STATE_FILE" "steps.$idx.env_deaths=$env_deaths" "steps.$idx.state=\"pending\"" >/dev/null
        if [ "$env_deaths" -gt "$ENV_DEATH_MAX" ]; then
          quarantine_step "$idx" "$phase" "env-death-budget-exhausted env_deaths=$env_deaths max=$ENV_DEATH_MAX rc=$rc" "$log"; continue
        fi
        local k=$((env_deaths - 1)); [ "$k" -le 10 ] || k=10
        local backoff=$(( ENV_BACKOFF_BASE * (1 << k) )); [ "$backoff" -le "$ENV_BACKOFF_CAP" ] || backoff="$ENV_BACKOFF_CAP"
        say "env-death phase=$phase rc=$rc env_deaths=$env_deaths/$ENV_DEATH_MAX backoff=${backoff}s (not a step failure)"
        sleep "$backoff"
        ;;
      75|76)
        wall_waits=$((wall_waits + 1))
        dstate "$STATE_FILE" "steps.$idx.wall_waits=$wall_waits" "steps.$idx.state=\"pending\"" >/dev/null
        if [ "$wall_waits" -gt "$WALL_WAIT_MAX" ]; then
          block_step "$idx" "$phase" "plan-wall-undecided waits=$wall_waits rc=$rc" "$rc" "$log"; continue
        fi
        say "wall-pending phase=$phase rc=$rc wait=$wall_waits/$WALL_WAIT_MAX sleep=${WALL_DEFER_SECS}s (budget-neutral)"
        sleep "$WALL_DEFER_SECS"
        ;;
      78)
        block_step "$idx" "$phase" "operator-input-required (rc 78: tuple drift, missing grant, or refused launch)" "$rc" "$log"
        ;;
      *)
        step_failure "$idx" "$phase" "$rc" "$log" "$stamp_before" "$stamp_after" "$fp_prev"
        ;;
    esac
  done
}

step_failure() {
  # $1 idx $2 phase $3 rc $4 log $5 stamp_before $6 stamp_after $7 fp_prev
  local idx="$1" phase="$2" rc="$3" log="$4" before="$5" after="$6" fp_prev="$7" fp attempts no_progress
  attempts="$(( $(q ".steps[$idx].attempts") + 1 ))"
  no_progress="$(q ".steps[$idx].no_progress")"
  fp="$(fingerprint "$rc" "$log")"
  if [ "$before" = "$after" ] && [ "$fp" = "$fp_prev" ]; then no_progress=$((no_progress + 1)); else no_progress=0; fi
  dstate "$STATE_FILE" "steps.$idx.attempts=$attempts" "steps.$idx.no_progress=$no_progress" "steps.$idx.fingerprint=\"$fp\"" "steps.$idx.state=\"pending\"" >/dev/null
  if [ "$no_progress" -ge "$NO_PROGRESS_MAX" ]; then
    quarantine_step "$idx" "$phase" "no-progress identical-failures=$no_progress rc=$rc" "$log"; return
  fi
  if [ "$attempts" -ge "$STEP_ATTEMPTS" ]; then
    quarantine_step "$idx" "$phase" "step-attempts-exhausted attempts=$attempts max=$STEP_ATTEMPTS rc=$rc" "$log"; return
  fi
  say "step-failed phase=$phase rc=$rc attempt=$attempts/$STEP_ATTEMPTS no_progress=$no_progress/$NO_PROGRESS_MAX backoff=${STEP_BACKOFF}s"
  sleep "$STEP_BACKOFF"
}

quarantine_step() {
  # $1 idx $2 phase $3 reason $4 log
  dstate "$STATE_FILE" "steps.$1.state=\"quarantined\"" "steps.$1.reason=\"$3\"" >/dev/null
  record_pending "$2" quarantined "$3" "$(q ".steps[$1].last_rc")" "$4"
  say "quarantined phase=$2 reason=\"$3\""
  after_blocked "$1" "$2"
}

block_step() {
  # $1 idx $2 phase $3 reason $4 rc $5 log
  dstate "$STATE_FILE" "steps.$1.state=\"blocked\"" "steps.$1.reason=\"$3\"" >/dev/null
  record_pending "$2" blocked "$3" "$4" "$5"
  say "blocked phase=$2 reason=\"$3\""
  after_blocked "$1" "$2"
}

after_blocked() {
  local idx="$1" phase="$2"
  if [ "$(q .on_blocked)" = park ]; then
    dstate "$STATE_FILE" 'state="parked"' >/dev/null
    life transition "$RUN" waiting parked >/dev/null 2>&1 || true
    say "parked run=$RUN at_phase=$phase pending=$STATE_DIR/drive-$RUN.pending.md -- resume with: drive.sh resume --run $RUN"
    exit 0
  fi
  dstate "$STATE_FILE" "index=$((idx + 1))" >/dev/null
  say "continue-next from_phase=$phase"
}

cmd_start() {
  local verb="" phases="" project="${GSD_PROJECT:-}" after_step="" on_blocked="" detach=0 run=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --verb) verb="${2:-}"; shift ;;
      --phases) phases="${2:-}"; shift ;;
      --project) project="${2:-}"; shift ;;
      --after-step) after_step="${2:-}"; shift ;;
      --on-blocked) on_blocked="${2:-}"; shift ;;
      --run) run="${2:-}"; shift ;;
      --detach) detach=1 ;;
      *) fail "usage unknown-arg $1" ;;
    esac; shift
  done
  case "$verb" in plan-phase|execute-phase) ;; *) fail "usage --verb must be plan-phase or execute-phase" ;; esac
  [[ "$phases" =~ ^([0-9]+)(-([0-9]+))?$ ]] || fail "usage --phases A-B"
  local first="${BASH_REMATCH[1]}" last="${BASH_REMATCH[3]:-${BASH_REMATCH[1]}}"
  [ "$first" -le "$last" ] || fail "usage --phases A-B needs A<=B"
  [ -z "$project" ] || safe_id "$project" || fail "usage --project unsafe"
  [ -n "$on_blocked" ] || { [ "$verb" = plan-phase ] && on_blocked=next || on_blocked=park; }
  case "$on_blocked" in next|park) ;; *) fail "usage --on-blocked next|park" ;; esac
  [ -n "$run" ] || run="drive-${project:-run}-$(date -u +%Y%m%dT%H%M%SZ)"
  safe_id "$run" || fail "usage --run unsafe"
  RUN="$run"; STATE_FILE="$(state_file "$RUN")"
  [ ! -f "$STATE_FILE" ] || fail "exists run=$RUN ($STATE_FILE) -- use resume"
  mkdir -p "$STATE_DIR"
  python3 - "$STATE_FILE" "$RUN" "$verb" "$project" "$after_step" "$on_blocked" "$first" "$last" <<'PY'
import json, sys, time
path, run, verb, project, after_step, on_blocked, first, last = sys.argv[1:]
steps = [{"phase": n, "run_id": f"{run}-p{n}", "state": "pending", "attempts": 0, "env_deaths": 0,
          "launches": 0, "no_progress": 0, "wall_waits": 0, "fingerprint": "", "head": "",
          "started_at": None, "last_rc": None, "reason": ""} for n in range(int(first), int(last) + 1)]
state = {"run_id": run, "verb": verb, "project": project or None, "after_step": after_step or None,
         "on_blocked": on_blocked, "steps": steps, "index": 0, "state": "running", "pid": None,
         "created_at": int(time.time()), "heartbeat": int(time.time())}
with open(path, "w") as fh:
    json.dump(state, fh, indent=2, sort_keys=True); fh.write("\n")
PY
  local argv='["scripts/gsd/drive.sh","resume","--run","'"$RUN"'"]'
  life launch "$RUN" "$argv" "{\"respawns\":0,\"env_deaths\":$ENV_DEATH_MAX}" "$$" >/dev/null 2>&1 \
    || say "lifecycle-record-unavailable run=$RUN (reconcile will not relaunch this supervisor)"
  local log="$STATE_DIR/drive-$RUN.log"
  if [ "$detach" -eq 1 ]; then
    local pid
    pid="$(detach_exec "$log" bash "$SCRIPT_DIR/drive.sh" resume --run "$RUN")"
    life set-pid "$RUN" "$pid" >/dev/null 2>&1 || true
    # The detached child owns drive state; a parent write races its updates.
    # Publish success only after it has recorded its identity.
    local ready=0 _poll
    for _poll in $(seq 1 100); do
      if [ "$(q '.pid // 0')" = "$pid" ]; then ready=1; break; fi
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    [ "$ready" -eq 1 ] || fail "startup-unconfirmed run=$RUN pid=$pid log=$log"
    say "started run=$RUN pid=$pid log=$log state=$STATE_FILE"
    return 0
  fi
  cmd_resume --run "$RUN"
}

cmd_resume() {
  local run=""
  while [ $# -gt 0 ]; do case "$1" in --run) run="${2:-}"; shift ;; *) fail "usage unknown-arg $1" ;; esac; shift; done
  safe_id "$run" || fail "usage --run required"
  RUN="$run"; STATE_FILE="$(state_file "$RUN")"
  [ -f "$STATE_FILE" ] || fail "missing-state run=$RUN"
  local prev state project
  prev="$(q '.pid // empty')"; state="$(q .state)"
  if [ "$state" = "done" ]; then say "already-done run=$RUN"; return 0; fi
  if [ -n "$prev" ] && [ "$prev" != "$$" ] && kill -0 "$prev" 2>/dev/null; then fail "already-running run=$RUN pid=$prev"; fi
  local argv='["scripts/gsd/drive.sh","resume","--run","'"$RUN"'"]'
  local lout
  if ! lout="$(life launch "$RUN" "$argv" "{\"respawns\":0,\"env_deaths\":$ENV_DEATH_MAX}" "$$" 2>&1)"; then
    case "$lout" in *LIFECYCLE:terminal*) fail "terminal run=$RUN ($lout)" ;; esac
  fi
  project="$(q '.project // empty')"
  if [ -n "$project" ]; then export GSD_PROJECT="$project"; else unset GSD_PROJECT; fi
  dstate "$STATE_FILE" "pid=$$" 'state="running"' >/dev/null
  say "resume run=$RUN pid=$$ index=$(q .index)/$(q '.steps|length')"
  run_loop
}

cmd_status() {
  local run="" f
  while [ $# -gt 0 ]; do case "$1" in --run) run="${2:-}"; shift ;; *) fail "usage unknown-arg $1" ;; esac; shift; done
  if [ -z "$run" ]; then
    f=""
    for cand in "$STATE_DIR"/drive-*.json; do
      [ -f "$cand" ] || continue
      [ -z "$f" ] || [ "$cand" -nt "$f" ] && f="$cand"
    done
    [ -n "$f" ] || { say "none"; return 0; }
    run="${f##*/drive-}"; run="${run%.json}"
  fi
  RUN="$run"; STATE_FILE="$(state_file "$RUN")"
  [ -f "$STATE_FILE" ] || { say "unknown run=$RUN"; return 1; }
  local st pid idx total phase alive age
  st="$(q .state)"; pid="$(q '.pid // 0')"; idx="$(q .index)"; total="$(q '.steps|length')"
  age=$(( $(date +%s) - $(q .heartbeat) ))
  if [ "$idx" -lt "$total" ]; then phase="$(q ".steps[$idx].phase")"; else phase=-; fi
  case "$st" in
    "done") alive="done" ;;
    parked) alive=parked ;;
    *) if num "$pid" && [ "$pid" -gt 0 ] && kill -0 "$pid" 2>/dev/null; then alive=alive; else alive=dead; fi ;;
  esac
  say "$alive run=$RUN pid=$pid step=$((idx + 1))/$total phase=$phase attempts=$(q ".steps[$idx].attempts // 0") env_deaths=$(q ".steps[$idx].env_deaths // 0") launches=$(q ".steps[$idx].launches // 0") heartbeat_age=${age}s pending=$(jq -r '[.steps[]|select(.state=="blocked" or .state=="quarantined")]|length' "$STATE_FILE")"
  [ "$alive" != dead ]
}

cmd_stop() {
  local run=""
  while [ $# -gt 0 ]; do case "$1" in --run) run="${2:-}"; shift ;; *) fail "usage unknown-arg $1" ;; esac; shift; done
  safe_id "$run" || fail "usage --run required"
  RUN="$run"; STATE_FILE="$(state_file "$RUN")"
  [ -f "$STATE_FILE" ] || fail "missing-state run=$RUN"
  local pid; pid="$(q '.pid // 0')"
  if num "$pid" && [ "$pid" -gt 0 ] && kill -0 "$pid" 2>/dev/null; then kill -TERM "$pid" 2>/dev/null || true; fi
  dstate "$STATE_FILE" 'state="stopped"' >/dev/null
  # terminal on purpose: an operator stop must not be undone by reconcile
  life transition "$RUN" failed operator-stop >/dev/null 2>&1 \
    || { life transition "$RUN" waiting operator-stop >/dev/null 2>&1 && life transition "$RUN" failed operator-stop >/dev/null 2>&1; } || true
  say "stopped run=$RUN pid=$pid"
}

verb="${1:-}"; shift || true
case "$verb" in
  start) cmd_start "$@" ;;
  resume) cmd_resume "$@" ;;
  status) cmd_status "$@" ;;
  stop) cmd_stop "$@" ;;
  -h|--help|"") usage; [ -n "$verb" ] ;;
  *) usage >&2; fail "usage unknown-verb $verb" ;;
esac
