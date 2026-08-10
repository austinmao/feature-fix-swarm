#!/usr/bin/env bash
# One bounded pass over durable lifecycle records.  It deliberately never sleeps.
set -uo pipefail

usage() { echo 'Usage: reconcile.sh'; }
fail() { printf 'RECONCILE:%s\n' "$*" >&2; exit 1; }

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
STATE_DIR="$ROOT/.planning/run-state"
LIFE="$ROOT/scripts/gsd/lifecycle.sh"
COORD="$ROOT/scripts/coord/coord.py"

[ "${1:-}" != "--help" ] || { usage; exit 0; }
if [ "${FFS_RECONCILE:-on}" = off ]; then echo 'RECONCILE:disabled'; exit 0; fi

claim_release() {
  local id="$1" generation="$2"
  python3 "$COORD" release "$id" --generation "$generation" >/dev/null 2>&1 || true
}

quarantined() {
  [ -f "$STATE_DIR/gsd-run.status" ] && grep -qx 'state=quarantined' "$STATE_DIR/gsd-run.status"
}

claim_id() {
  # coord accepts at most 64 bytes.  Do not silently truncate a run id: that
  # would turn two distinct records into the same coordination key.
  local run="$1" key="reconcile-$1"
  [ "${#key}" -le 64 ] && printf '%s\n' "$key"
}

stale_running() {
  local record="$1" pid launched now stale
  pid="$(jq -r '.child_pid // .launcher_pid // 0' "$record" 2>/dev/null)"
  launched="$(jq -r '.launched_at // 0' "$record" 2>/dev/null)"
  stale="${FFS_RECONCILE_STALE_SECS:-300}"
  [[ "$pid" =~ ^[0-9]+$ && "$launched" =~ ^[0-9]+$ && "$stale" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null && return 1
  now="$(date +%s)"
  [ "$((now - launched))" -ge "$stale" ]
}

due() {
  local record="$1" type wake now rc
  type="$(jq -r '.wake_condition.type' "$record")"
  case "$type" in
    time)
      wake="$(jq -r '.wake_at // .wake_condition.params.wake_at // -1' "$record")"
      now="$(date +%s)"; [ "$wake" -le "$now" ]
      ;;
    wall-decided)
      bash "$ROOT/scripts/gsd/plan-wall.sh" --await 1 "$(jq -r '.wake_condition.params.phase_dir // empty' "$record")" >/dev/null 2>&1; rc=$?
      [ "$rc" -eq 0 ]
      ;;
    ci-completed)
      bash "$ROOT/scripts/gsd/ci-watch.sh" evaluate --run-id "$(jq -r .run_id "$record")" >/dev/null 2>&1 || true
      [ "$(jq -r .state "$record")" = runnable ]
      ;;
    *) return 1 ;;
  esac
}

# life: run lifecycle.sh with its CWD at the record's own store root —
# lifecycle resolves its record dir from the invoking CWD's git toplevel, so
# a worktree-store record must be mutated from inside that worktree.
life() { (cd "$rec_root" && bash "$LIFE" "$@"); }

shopt -s nullglob
# Records live in this checkout's store AND in run-worktree stores (the
# runner cd's into the worktree before the drive, so session-wake and
# executor checkpoints land there).
records=("$STATE_DIR"/lifecycle-*.json "$ROOT"/.claude/worktrees/*/.planning/run-state/lifecycle-*.json)
shopt -u nullglob
for record in "${records[@]}"; do
  run="${record##*/lifecycle-}"; run="${run%.json}"
  rec_root="${record%/.planning/run-state/*}"
  if ! life validate "$run" >/dev/null 2>&1; then echo "RECONCILE:invalid-record run=$run"; continue; fi
  state="$(jq -r .state "$record")"
  case "$state" in
    waiting) eligible=0 ;;
    # runnable = wake condition already satisfied (legacy/ci-watch writes it);
    # launch directly — due() has no verdict for it and would strand the record
    runnable) eligible=1 ;;
    running) stale_running "$record" || { echo "RECONCILE:still-waiting run=$run"; continue; }; eligible=1 ;;
    *) echo "RECONCILE:still-waiting run=$run"; continue ;;
  esac
  if quarantined; then echo "RECONCILE:quarantined run=$run"; continue; fi
  if [ "$eligible" -eq 0 ] && ! due "$record"; then echo "RECONCILE:still-waiting run=$run"; continue; fi
  # ci-watch changes the lifecycle record from waiting to runnable. Re-read
  # after evaluator side effects rather than trusting the old JSON snapshot.
  if ! life validate "$run" >/dev/null 2>&1; then echo "RECONCILE:invalid-record run=$run"; continue; fi
  state="$(jq -r .state "$record")"
  case "$state" in waiting|runnable|running) ;; *) echo "RECONCILE:still-waiting run=$run"; continue;; esac
  claim="$(claim_id "$run" || true)"; [ -n "$claim" ] || { echo "RECONCILE:invalid-run-id run=$run"; continue; }
  result="$(python3 "$COORD" claim "$claim" 2>&1)"; rc=$?
  if [ "$rc" -eq 3 ]; then echo "RECONCILE:claim-held run=$run"; continue; fi
  if [ "$rc" -ne 0 ]; then echo "RECONCILE:coord-error run=$run"; continue; fi
  generation="$(printf '%s\n' "$result" | sed -n 's/^CLAIM-OK generation=//p' | head -1)"
  # A stale running record has already spent its previous budget.  Reset it to
  # waiting then relaunch without a second charge: the dead child made no
  # progress and the stale-liveness guard is the double-launch protection.
  relaunch_args=("$run" reconcile)
  if [ "$state" = running ]; then
    life transition "$run" waiting stale-launcher >/dev/null || { echo "RECONCILE:invalid-record run=$run"; claim_release "$claim" "$generation"; continue; }
    relaunch_args+=(--no-charge)
  fi
  relaunch_err="$(mktemp "${TMPDIR:-/tmp}/ffs-reconcile-err.XXXXXX")"
  argv="$(life relaunch "${relaunch_args[@]}" 2>"$relaunch_err")"; rc=$?
  relaunch_err_text="$(cat "$relaunch_err" 2>/dev/null)"; rm -f "$relaunch_err"
  if [ "$rc" -ne 0 ]; then
    if [[ "$argv$relaunch_err_text" == *budget-exhausted* ]]; then echo "RECONCILE:budget-exhausted run=$run"; else echo "RECONCILE:invalid-record run=$run"; fi
    claim_release "$claim" "$generation"; continue
  fi
  mapfile -t command < <(printf '%s' "$argv" | jq -r '.[]')
  [ "${#command[@]}" -gt 0 ] || { echo "RECONCILE:invalid-argv run=$run"; claim_release "$claim" "$generation"; continue; }
  case "${command[0]}" in /*) ;; *) command[0]="$ROOT/${command[0]}" ;; esac
  executable="$(realpath "${command[0]}" 2>/dev/null || true)"
  case "$executable" in "$ROOT/scripts/gsd/"*) ;; *) echo "RECONCILE:invalid-argv run=$run"; claim_release "$claim" "$generation"; continue;; esac
  log="$STATE_DIR/reconcile-$run.log"
  # the record's run id must reach the relaunched runner — argv carries no env
  GSD_RUN_ID="$run" nohup "${command[@]}" >"$log" 2>&1 < /dev/null & child=$!
  disown "$child" 2>/dev/null || true
  life set-pid "$run" "$child" >/dev/null || { life transition "$run" failed launch-failed >/dev/null 2>&1 || true; echo "RECONCILE:launch-failed run=$run"; claim_release "$claim" "$generation"; continue; }
  echo "RECONCILE:relaunched run=$run pid=$child"
  claim_release "$claim" "$generation"
done
