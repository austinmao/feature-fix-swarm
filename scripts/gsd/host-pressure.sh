#!/usr/bin/env bash
# Memory-aware admission for autonomous drives.
#
# A drive launched into a host that is already deep in swap is the drive most
# likely to be reaped (Claude Code kills every background task "because the
# system is running low on memory"; spec-443 run 2026-10-25 died this way).
# This lever reports pressure so callers can defer the launch instead:
#
#   host-pressure.sh check             rc 0 ok | rc 75 high | rc 0 unknown
#   host-pressure.sh wait [--max-secs N]
#                                      block with backoff while high; rc 0 when
#                                      ok or when the wait budget is spent
#                                      (launch anyway, loudly) -- never a stop.
#
# Pressure is HIGH when swap use >= FFS_PRESSURE_SWAP_PCT (85) AND free memory
# < FFS_PRESSURE_FREE_PCT (10), or when free memory < FFS_PRESSURE_FREE_CRIT_PCT
# (5) alone. Swap alone is not enough: macOS grows swap on demand and a host
# can sit at 90% swap for days while still serving launches.
#
# Observation only -- this script never kills anything to free memory.
set -uo pipefail

usage() { echo 'Usage: host-pressure.sh check | wait [--max-secs N]'; }

SWAP_PCT_MAX="${FFS_PRESSURE_SWAP_PCT:-85}"
FREE_PCT_MIN="${FFS_PRESSURE_FREE_PCT:-10}"
FREE_PCT_CRIT="${FFS_PRESSURE_FREE_CRIT_PCT:-5}"
MAX_WAIT="${FFS_PRESSURE_MAX_WAIT_SECS:-7200}"
BACKOFF_BASE="${FFS_PRESSURE_BACKOFF_BASE_SECS:-60}"
BACKOFF_CAP="${FFS_PRESSURE_BACKOFF_CAP_SECS:-900}"

num() { [[ "${1:-}" =~ ^[0-9]+$ ]]; }
for v in SWAP_PCT_MAX FREE_PCT_MIN FREE_PCT_CRIT MAX_WAIT BACKOFF_BASE BACKOFF_CAP; do
  num "${!v}" || { echo "HOST-PRESSURE:invalid-env $v=${!v}" >&2; exit 2; }
done

# read_pressure -> prints "swap_pct free_pct" or "unknown"
read_pressure() {
  local swap_pct="" free_pct=""
  case "${FFS_HOST_PRESSURE_FIXTURE:-}" in
    ok) echo "0 50"; return 0 ;;
    high) echo "95 3"; return 0 ;;
    "") ;;
    *) echo "HOST-PRESSURE:invalid-fixture ${FFS_HOST_PRESSURE_FIXTURE}" >&2; return 2 ;;
  esac
  if [ -r /proc/meminfo ]; then
    swap_pct="$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{if(t>0) printf "%d", (t-f)*100/t; else print 0}' /proc/meminfo)"
    free_pct="$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{if(t>0) printf "%d", a*100/t; else print ""}' /proc/meminfo)"
  elif command -v sysctl >/dev/null 2>&1 && sysctl -n vm.swapusage >/dev/null 2>&1; then
    swap_pct="$(sysctl -n vm.swapusage | parse_swapusage)"
    if command -v memory_pressure >/dev/null 2>&1; then
      free_pct="$(memory_pressure -Q 2>/dev/null | sed -n 's/.*free percentage: *\([0-9][0-9]*\)%.*/\1/p' | head -1)"
    fi
    if ! num "$free_pct" && command -v vm_stat >/dev/null 2>&1; then
      free_pct="$(vm_stat | parse_vm_stat "$(sysctl -n hw.memsize 2>/dev/null || echo 0)")"
    fi
  fi
  if num "$swap_pct" && num "$free_pct"; then echo "$swap_pct $free_pct"; else echo unknown; fi
}

# "total = 17408.00M  used = 16312.94M  free = 1095.06M  (encrypted)" -> 93
parse_swapusage() {
  awk '{
    for (i=1;i<=NF;i++) { if ($i=="total") t=$(i+2); if ($i=="used") u=$(i+2) }
    gsub(/[^0-9.]/,"",t); gsub(/[^0-9.]/,"",u)
    if (t+0 > 0) printf "%d", (u/t)*100; else print 0 }'
}

# vm_stat pages (free + inactive + speculative) / hw.memsize -> free pct
parse_vm_stat() {
  awk -v memsize="$1" '
    /page size of/ { ps=$8 }
    /^Pages free:/ { f=$3 } /^Pages inactive:/ { ia=$3 } /^Pages speculative:/ { sp=$3 }
    END { gsub(/\./,"",f); gsub(/\./,"",ia); gsub(/\./,"",sp); if (ps=="" ) ps=16384
          if (memsize+0 > 0) printf "%d", (f+ia+sp)*ps*100/memsize; else print "" }'
}

classify() {
  # $1 swap_pct $2 free_pct -> 0 ok | 75 high
  local swap="$1" free="$2"
  if [ "$free" -lt "$FREE_PCT_CRIT" ]; then return 75; fi
  if [ "$swap" -ge "$SWAP_PCT_MAX" ] && [ "$free" -lt "$FREE_PCT_MIN" ]; then return 75; fi
  return 0
}

check_once() {
  local reading rc
  [ "${FFS_HOST_PRESSURE:-on}" != off ] || { echo "HOST-PRESSURE:disabled"; return 0; }
  reading="$(read_pressure)" || return 2
  if [ "$reading" = unknown ]; then echo "HOST-PRESSURE:unknown"; return 0; fi
  set -- $reading
  if classify "$1" "$2"; then echo "HOST-PRESSURE:ok swap_pct=$1 free_pct=$2"; return 0; fi
  echo "HOST-PRESSURE:high swap_pct=$1 free_pct=$2"; return 75
}

verb="${1:-}"; shift || true
case "$verb" in
  check) check_once; exit $? ;;
  wait)
    max="$MAX_WAIT"
    while [ $# -gt 0 ]; do
      case "$1" in
        --max-secs) shift; max="${1:-}"; num "$max" || { echo 'HOST-PRESSURE:invalid --max-secs' >&2; exit 2; } ;;
        *) usage >&2; exit 2 ;;
      esac; shift
    done
    waited=0; k=0
    while :; do
      out="$(check_once)"; rc=$?
      echo "$out"
      [ "$rc" -eq 75 ] || exit "$rc"
      if [ "$waited" -ge "$max" ]; then
        echo "HOST-PRESSURE:wait-exhausted waited=${waited}s max=${max}s -- launching anyway" >&2
        exit 0
      fi
      sleep_for=$(( BACKOFF_BASE * (1 << k) ))
      [ "$sleep_for" -le "$BACKOFF_CAP" ] || sleep_for="$BACKOFF_CAP"
      remaining=$(( max - waited ))
      [ "$sleep_for" -le "$remaining" ] || sleep_for="$remaining"
      echo "HOST-PRESSURE:waiting sleep=${sleep_for}s waited=${waited}s" >&2
      sleep "$sleep_for"
      waited=$(( waited + sleep_for )); [ "$k" -lt 10 ] && k=$(( k + 1 ))
    done
    ;;
  --help|-h|"") usage; [ -n "$verb" ] ;;
  *) usage >&2; exit 2 ;;
esac
