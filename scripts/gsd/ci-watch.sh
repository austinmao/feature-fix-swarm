#!/usr/bin/env bash
set -uo pipefail
usage(){ echo 'Usage: ci-watch.sh evaluate --run-id ID'; }
fail(){ printf 'CI-WATCH:%s\n' "$1" >&2; exit "${2:-1}"; }
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"; LIFE="$ROOT/scripts/gsd/lifecycle.sh"
. "$ROOT/scripts/gsd/run-bounded.sh"
MAX="${FFS_CI_RERUN_MAX:-2}"; ERRMAX="${FFS_CI_GH_ERR_MAX:-5}"
evaluate(){
 local run="$1" rec id val status attempt last log reservation
 rec="$(bash "$LIFE" show "$run")" || fail missing-record
 id="$(printf %s "$rec" | python3 -c 'import json,sys; print(json.load(sys.stdin)["wake_condition"]["params"]["databaseId"])')" || fail missing-database-id
 val="$(run_bounded 60 gh run view "$id" --json databaseId,status,conclusion,attempt 2>/dev/null)" || { bash "$LIFE" ci-gh-error "$run" "$ERRMAX" >/dev/null; echo 'CI-WATCH:gh-error idle'; return; }
 status="$(printf %s "$val" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))')"; [ "$status" = completed ] || { echo 'CI-WATCH:pending'; return; }
 attempt="$(printf %s "$val" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("attempt",0))')"; last="$(printf %s "$rec" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ci",{}).get("last_classified_attempt",0))')"; [ "$attempt" -gt "$last" ] || { echo 'CI-WATCH:awaiting-attempt'; return; }
 if printf %s "$val" | grep -qi 'success'; then bash "$LIFE" ci-ready "$run" >/dev/null; echo 'CI-WATCH:pass'; return; fi
 log="$(run_bounded 120 gh run view "$id" --log-failed 2>/dev/null)" || { echo 'CI-WATCH:gh-error idle'; return; }
 if printf %s "$log" | grep -Eiq 'runner-lost|system cancellation|startup_failure|network|dns|timeout|429|disk-space'; then
  reservation="$(bash "$LIFE" ci-reserve "$run" "$attempt" "$MAX")"; case "$reservation" in *exhausted*) echo 'CI-WATCH:rerun-exhausted'; return 1;; *cas-lost*) echo 'CI-WATCH:awaiting-attempt'; return;; esac
  run_bounded 60 gh run rerun "$id" --failed >/dev/null 2>&1 || { echo 'CI-WATCH:gh-error idle'; return; }; bash "$LIFE" ci-complete-rerun "$run" >/dev/null; echo "CI-WATCH:rerun:$id"
 else bash "$LIFE" ci-failed "$run" test-failure >/dev/null; echo 'CI-WATCH:test-failure'; return 1; fi
}
watch(){
 local id="$1" interval="${FFS_CI_WATCH_BASE_SECS:-30}" ceiling="${FFS_CI_WATCH_CEILING_SECS:-600}" max="${FFS_CI_WATCH_MAX_SECS:-7200}" start now rc
 start="$(date +%s)"
 while :; do
  now="$(date +%s)"; [ $((now-start)) -lt "$max" ] || { echo 'CI-WATCH:deadline'; return 75; }
  echo "CI-WATCH:poll databaseId=$id interval=$interval"
  run_bounded 60 gh run view "$id" --json databaseId,status,conclusion,attempt >/dev/null 2>&1; rc=$?
  [ "$rc" -eq 0 ] && return 0
  sleep "$interval"; interval=$((interval * 2)); [ "$interval" -le "$ceiling" ] || interval="$ceiling"
 done
}
case "${1:-}" in
 evaluate) [ "${2:-}" = --run-id ] || fail usage; evaluate "$3";;
 watch) [ "${2:-}" = --database-id ] || fail usage; watch "$3";;
 *) usage; fail usage;;
esac
