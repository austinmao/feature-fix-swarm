#!/usr/bin/env bash
# queue-guard.sh — machine-readable allow/stop seam for the serial land queue.
#
# The guard is a pure decision lever: it inspects caps and operator markers
# and emits exactly one verdict line.  It never touches a collector, vendor
# CLI, or effect-child boundary — the runner consults it immediately before
# STARTING every external effect (binding e846ec0c).
#
# Verdict grammar (stdout, one line):
#   ALLOW               rc 0
#   STOP:operator-stop  rc 3   <store>/STOP marker present
#   STOP:max-items      rc 3   more than 10 queue items       (REQ-204)
#   STOP:round-cap      rc 3   more than 2 rounds on an item  (REQ-204)
#   STOP:item-wall      rc 3   item wall clock beyond 90 min  (REQ-204)
#   STOP:queue-wall     rc 3   queue wall clock beyond 8 h    (REQ-204)
#   DRAIN-CONSUMED      rc 0   drain-consume removed a DRAIN marker
#   NONE                rc 4   drain-consume found no marker
set -uo pipefail

ROUND_CAP=2
ITEM_WALL_SECONDS=5400
QUEUE_WALL_SECONDS=28800
MAX_ITEMS=10

usage() {
  echo "usage: queue-guard.sh allow --store DIR --items N --queue-started EPOCH [--item-started EPOCH] [--round N] [--now EPOCH]" >&2
  echo "       queue-guard.sh drain-consume --store DIR" >&2
  echo "       queue-guard.sh --contract-probe" >&2
  exit 2
}

[ $# -ge 1 ] || usage
if [ "$1" = "--contract-probe" ]; then
  exit 0
fi

cmd="$1"; shift
store="" items=0 queue_started="" item_started="" round=1 now=""
while [ $# -gt 0 ]; do
  case "$1" in
    --store) store="${2:?--store requires a value}"; shift 2 ;;
    --items) items="${2:?--items requires a value}"; shift 2 ;;
    --queue-started) queue_started="${2:?--queue-started requires a value}"; shift 2 ;;
    --item-started) item_started="${2:?--item-started requires a value}"; shift 2 ;;
    --round) round="${2:?--round requires a value}"; shift 2 ;;
    --now) now="${2:?--now requires a value}"; shift 2 ;;
    *) usage ;;
  esac
done
case "$now" in ''|*[!0-9]*) now="$(date +%s)" ;; esac

case "$cmd" in
  allow)
    [ -n "$store" ] || usage
    if [ -e "$store/STOP" ]; then
      echo "STOP:operator-stop"; exit 3
    fi
    case "$items" in ''|*[!0-9]*) usage ;; esac
    if [ "$items" -gt "$MAX_ITEMS" ]; then
      echo "STOP:max-items"; exit 3
    fi
    case "$round" in ''|*[!0-9]*) usage ;; esac
    if [ "$round" -gt "$ROUND_CAP" ]; then
      echo "STOP:round-cap"; exit 3
    fi
    if [ -n "$item_started" ]; then
      case "$item_started" in *[!0-9]*) usage ;; esac
      if [ $((now - item_started)) -gt "$ITEM_WALL_SECONDS" ]; then
        echo "STOP:item-wall"; exit 3
      fi
    fi
    if [ -n "$queue_started" ]; then
      case "$queue_started" in *[!0-9]*) usage ;; esac
      if [ $((now - queue_started)) -gt "$QUEUE_WALL_SECONDS" ]; then
        echo "STOP:queue-wall"; exit 3
      fi
    fi
    echo "ALLOW"; exit 0 ;;
  drain-consume)
    # 71c46cda: the DRAIN marker is per-queue-store, honored once, and
    # consumed here — the runner only calls this while holding the queue lock.
    [ -n "$store" ] || usage
    if [ -e "$store/DRAIN" ]; then
      rm -f -- "$store/DRAIN" || { echo "QUEUE-ERROR:store"; exit 70; }
      echo "DRAIN-CONSUMED"; exit 0
    fi
    echo "NONE"; exit 4 ;;
  *) usage ;;
esac
