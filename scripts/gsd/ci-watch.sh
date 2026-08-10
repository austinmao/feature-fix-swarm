#!/usr/bin/env bash
set -uo pipefail
usage(){ echo 'Usage: ci-watch.sh evaluate --run-id ID'; }
fail(){ printf 'CI-WATCH:%s\n' "$1" >&2; exit "${2:-1}"; }
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; LIFE="$ROOT/scripts/gsd/lifecycle.sh"
. "$ROOT/scripts/gsd/run-bounded.sh"
MAX="${FFS_CI_RERUN_MAX:-2}"; ERRMAX="${FFS_CI_GH_ERR_MAX:-5}"
evaluate(){
 local run="$1" rec id val status attempt last pending log reservation
 rec="$(bash "$LIFE" show "$run")" || fail missing-record
 id="$(printf %s "$rec" | python3 -c 'import json,sys; print(json.load(sys.stdin)["wake_condition"]["params"]["databaseId"])')" || fail missing-database-id
 val="$(run_bounded 60 gh run view "$id" --json databaseId,status,conclusion,attempt 2>/dev/null)" || { bash "$LIFE" ci-gh-error "$run" "$ERRMAX" >/dev/null; echo 'CI-WATCH:gh-error idle'; return; }
 bash "$LIFE" ci-probe-success "$run" >/dev/null
 status="$(printf %s "$val" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))')"; [ "$status" = completed ] || { echo 'CI-WATCH:pending'; return; }
 attempt="$(printf %s "$val" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("attempt",0))')"; last="$(printf %s "$rec" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ci",{}).get("last_classified_attempt",0))')"; pending="$(printf %s "$rec" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ci",{}).get("rerun_pending",False))')"
 if [ "$pending" = True ]; then
  # A crash after GitHub accepted the rerun leaves pending set, but an
  # advanced attempt proves it happened; clear it instead of firing twice.
  if [ "$attempt" -gt "$last" ]; then bash "$LIFE" ci-complete-rerun "$run" >/dev/null; echo 'CI-WATCH:rerun-observed'; return; fi
  run_bounded 60 gh run rerun "$id" --failed >/dev/null 2>&1 || { bash "$LIFE" ci-refund-rerun "$run" >/dev/null; bash "$LIFE" ci-gh-error "$run" "$ERRMAX" >/dev/null; echo 'CI-WATCH:gh-error idle'; return; }
  bash "$LIFE" ci-complete-rerun "$run" >/dev/null; echo "CI-WATCH:rerun:$id"; return
 fi
 [ "$attempt" -gt "$last" ] || { echo 'CI-WATCH:awaiting-attempt'; return; }
 if printf %s "$val" | grep -qi 'success'; then bash "$LIFE" ci-ready "$run" >/dev/null; echo 'CI-WATCH:pass'; return; fi
 log="$(run_bounded 120 gh run view "$id" --log-failed 2>/dev/null)" || { echo 'CI-WATCH:gh-error idle'; return; }
 if printf %s "$log" | grep -Eiq 'runner-lost|system cancellation|startup_failure|network|dns|timeout|429|disk-space'; then
  reservation="$(bash "$LIFE" ci-reserve "$run" "$attempt" "$MAX")"; case "$reservation" in *exhausted*) echo 'CI-WATCH:rerun-exhausted'; return 1;; *cas-lost*) echo 'CI-WATCH:awaiting-attempt'; return;; *ci-pending*) :;; esac
  run_bounded 60 gh run rerun "$id" --failed >/dev/null 2>&1 || { bash "$LIFE" ci-refund-rerun "$run" >/dev/null; bash "$LIFE" ci-gh-error "$run" "$ERRMAX" >/dev/null; echo 'CI-WATCH:gh-error idle'; return; }; bash "$LIFE" ci-complete-rerun "$run" >/dev/null; echo "CI-WATCH:rerun:$id"
 else bash "$LIFE" ci-failed "$run" test-failure >/dev/null; echo 'CI-WATCH:test-failure'; return 1; fi
}
watch(){
 local id="$1" interval="${FFS_CI_WATCH_BASE_SECS:-30}" ceiling="${FFS_CI_WATCH_CEILING_SECS:-600}" max="${FFS_CI_WATCH_MAX_SECS:-7200}" start now remaining val status conclusion log rc
 start="$(date +%s)"
 while :; do
  now="$(date +%s)"; remaining=$((max-(now-start))); [ "$remaining" -gt 0 ] || { echo 'CI-WATCH:deadline'; return 75; }
  echo "CI-WATCH:poll databaseId=$id interval=$interval"
  val="$(run_bounded "$(( remaining < 60 ? remaining : 60 ))" gh run view "$id" --json databaseId,status,conclusion,attempt 2>/dev/null)"; rc=$?
  if [ "$rc" -eq 0 ]; then
   status="$(printf %s "$val" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))')"
   conclusion="$(printf %s "$val" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("conclusion") or "")')"
   if [ "$status" = completed ]; then
    if printf %s "$conclusion" | grep -qi success; then echo 'CI-WATCH:pass'; return 0; fi
    log="$(run_bounded "$(( remaining < 120 ? remaining : 120 ))" gh run view "$id" --log-failed 2>/dev/null)" || log=''
    if printf %s "$log" | grep -Eiq 'runner-lost|system cancellation|startup_failure|network|dns|timeout|429|disk-space'; then echo 'CI-WATCH:pending'; else echo 'CI-WATCH:test-failure'; return 1; fi
   else echo 'CI-WATCH:pending'; fi
  else echo 'CI-WATCH:gh-error idle'; fi
  now="$(date +%s)"; remaining=$((max-(now-start))); [ "$remaining" -gt 0 ] || { echo 'CI-WATCH:deadline'; return 75; }
  [ "$interval" -le "$remaining" ] || interval="$remaining"; sleep "$interval"; interval=$((interval * 2)); [ "$interval" -le "$ceiling" ] || interval="$ceiling"
 done
}
case "${1:-}" in
 evaluate) [ "${2:-}" = --run-id ] || fail usage; evaluate "$3";;
 watch) [ "${2:-}" = --database-id ] || fail usage; watch "$3";;
 *) usage; fail usage;;
esac
