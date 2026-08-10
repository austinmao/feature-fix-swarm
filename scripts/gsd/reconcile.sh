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

shopt -s nullglob
records=("$STATE_DIR"/lifecycle-*.json)
shopt -u nullglob
for record in "${records[@]}"; do
  run="${record##*/lifecycle-}"; run="${run%.json}"
  if ! bash "$LIFE" validate "$run" >/dev/null 2>&1; then echo "RECONCILE:invalid-record run=$run"; continue; fi
  state="$(jq -r .state "$record")"
  case "$state" in waiting|runnable) ;; *) echo "RECONCILE:still-waiting run=$run"; continue;; esac
  if quarantined; then echo "RECONCILE:quarantined run=$run"; continue; fi
  if ! due "$record"; then echo "RECONCILE:still-waiting run=$run"; continue; fi
  claim="reconcile-$run"; [ "${#claim}" -le 64 ] || { echo "RECONCILE:invalid-run-id run=$run"; continue; }
  result="$(python3 "$COORD" claim "$claim" 2>&1)"; rc=$?
  if [ "$rc" -eq 3 ]; then echo "RECONCILE:claim-held run=$run"; continue; fi
  if [ "$rc" -ne 0 ]; then echo "RECONCILE:coord-error run=$run"; continue; fi
  generation="$(printf '%s\n' "$result" | sed -n 's/^CLAIM-OK generation=//p' | head -1)"
  argv="$(bash "$LIFE" relaunch "$run" reconcile)"; rc=$?
  if [ "$rc" -ne 0 ]; then
    if [[ "$argv" == *budget-exhausted* ]]; then echo "RECONCILE:budget-exhausted run=$run"; else echo "RECONCILE:invalid-record run=$run"; fi
    claim_release "$claim" "$generation"; continue
  fi
  mapfile -t command < <(printf '%s' "$argv" | jq -r '.[]')
  executable="$(realpath "${command[0]}" 2>/dev/null || true)"
  case "$executable" in "$ROOT/scripts/gsd/"*) ;; *) echo "RECONCILE:invalid-argv run=$run"; claim_release "$claim" "$generation"; continue;; esac
  log="$STATE_DIR/reconcile-$run.log"
  nohup "${command[@]}" >"$log" 2>&1 < /dev/null & child=$!
  disown "$child" 2>/dev/null || true
  bash "$LIFE" set-pid "$run" "$child" >/dev/null || { echo "RECONCILE:launch-failed run=$run"; claim_release "$claim" "$generation"; continue; }
  echo "RECONCILE:relaunched run=$run pid=$child"
  claim_release "$claim" "$generation"
done
