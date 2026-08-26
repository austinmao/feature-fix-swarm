#!/usr/bin/env bash
# queue-guard.sh — machine-readable allow/stop seam for the serial land queue.
#
# The guard is a pure decision lever: it inspects caps, operator markers,
# failure history, and subprocess observations, and emits exactly one verdict
# line.  It never touches a collector, vendor CLI, or effect-child boundary —
# the runner consults it immediately before STARTING every external effect
# (binding e846ec0c).
#
# Verdict grammar (stdout, one line):
#   ALLOW                        rc 0
#   STOP:operator-stop           rc 3   <store>/STOP marker present
#   STOP:max-items               rc 3   more than 10 queue items      (REQ-204)
#   STOP:round-cap               rc 3   more than 2 rounds on an item (REQ-204)
#   STOP:item-wall               rc 3   item wall clock beyond 90 min (REQ-204)
#   STOP:queue-wall              rc 3   queue wall clock beyond 8 h   (REQ-204)
#   DRAIN-CONSUMED               rc 0   drain-consume removed a DRAIN marker
#   NONE                         rc 4   drain-consume found no marker
#   PROGRESS-OK                  rc 0   note-failure: first observation (REQ-205)
#   BLOCKED:no-progress          rc 6   note-failure: repeated signature
#   <class>                      rc 0   classify-subprocess (REQ-206)
#   RECORDED:systemic:<class>    rc 0   record: first consecutive systemic
#   RECORDED:reset               rc 0   record: local/success reset
#   QUEUE-ABORTED:systemic:<c>   rc 5   record: two consecutive systemic
set -uo pipefail

ROUND_CAP=2
ITEM_WALL_SECONDS=5400
QUEUE_WALL_SECONDS=28800
MAX_ITEMS=10

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATES="$SCRIPT_DIR/../../lib/gates.py"

usage() {
  echo "usage: queue-guard.sh allow --store DIR --items N --queue-started EPOCH [--item-started EPOCH] [--round N] [--now EPOCH]" >&2
  echo "       queue-guard.sh drain-consume --store DIR" >&2
  echo "       queue-guard.sh classify-subprocess --boundary NAME --rc N --stderr-file FILE" >&2
  echo "       queue-guard.sh record --store DIR --queue-id ID --class CLASS" >&2
  echo "       queue-guard.sh note-failure --queue-id ID --item ITEM --gate GATE --stderr-file FILE" >&2
  echo "       queue-guard.sh --contract-probe" >&2
  exit 2
}

[ $# -ge 1 ] || usage
if [ "$1" = "--contract-probe" ]; then
  exit 0
fi

cmd="$1"; shift
store="" items=0 queue_started="" item_started="" round=1 now=""
boundary="" rc="" errfile="" queue_id="" class="" item="" gate=""
while [ $# -gt 0 ]; do
  case "$1" in
    --store) store="${2:?--store requires a value}"; shift 2 ;;
    --items) items="${2:?--items requires a value}"; shift 2 ;;
    --queue-started) queue_started="${2:?--queue-started requires a value}"; shift 2 ;;
    --item-started) item_started="${2:?--item-started requires a value}"; shift 2 ;;
    --round) round="${2:?--round requires a value}"; shift 2 ;;
    --now) now="${2:?--now requires a value}"; shift 2 ;;
    --boundary) boundary="${2:?--boundary requires a value}"; shift 2 ;;
    --rc) rc="${2:?--rc requires a value}"; shift 2 ;;
    --stderr-file) errfile="${2:?--stderr-file requires a value}"; shift 2 ;;
    --queue-id) queue_id="${2:?--queue-id requires a value}"; shift 2 ;;
    --class) class="${2:?--class requires a value}"; shift 2 ;;
    --item) item="${2:?--item requires a value}"; shift 2 ;;
    --gate) gate="${2:?--gate requires a value}"; shift 2 ;;
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
  classify-subprocess)
    # REQ-206: closed boundary/rc/signature precedence.  Every unknown or
    # ambiguous observation routes to local so arbitrary stderr text can
    # never increment the systemic breaker (T-02-08).
    case "$boundary" in
      ci-watch|gates|gh|reviewer|implement|finalizer|git) ;;
      *) usage ;;
    esac
    case "$rc" in ''|*[!0-9]*) usage ;; esac
    [ -n "$errfile" ] && [ -r "$errfile" ] || usage
    # Bounded, lowercased, whitespace-squeezed view; anchors match at line
    # start only, so signature text buried mid-line stays local.
    norm="$(head -c 65536 -- "$errfile" | head -n 40 \
      | tr '[:upper:]' '[:lower:]' | sed -E 's/[[:space:]]+/ /g')"
    sig() { printf '%s\n' "$norm" | grep -Eq "$1"; }
    if [ "$boundary" = "ci-watch" ] && [ "$rc" -eq 124 ]; then
      echo "local:ci-timeout"; exit 0
    fi
    if [ "$boundary" = "gates" ] && [ "$rc" -eq 75 ] \
        && grep -q '^GATES-STORE-ERROR' -- "$errfile"; then
      echo "store-error"; exit 0
    fi
    if [ "$boundary" = "gh" ] \
        && sig '^(gh: )?not logged in|^(gh: )?authentication required|^http 401([ :]|$)|^error: authentication required'; then
      echo "gh-auth"; exit 0
    fi
    if [ "$boundary" = "gh" ] || [ "$boundary" = "reviewer" ]; then
      if sig '^(curl: \([0-9]+\) )?could not resolve host|^dns resolution (failed|error)|^connection (refused|reset( by peer)?|timed out)|^tls handshake (failure|failed|timeout|error)'; then
        echo "network"; exit 0
      fi
    fi
    if [ "$boundary" = "reviewer" ]; then
      case "$rc" in 124|126|127) echo "reviewer-unreachable"; exit 0 ;; esac
    fi
    echo "local"; exit 0 ;;
  record)
    # Breaker accounting.  Binding 6e4616bc (stricter reading): ANY two
    # consecutive enumerated systemic failures abort, class-agnostic; every
    # success/local observation resets the consecutive sequence.  Only the
    # classifier's four literal systemic tokens are ever counted.
    [ -n "$store" ] && [ -n "$queue_id" ] && [ -n "$class" ] || usage
    case "$queue_id" in */*|.*|'') usage ;; esac
    bfile="$store/$queue_id.breaker"
    case "$class" in
      reviewer-unreachable|store-error|gh-auth|network)
        count=0
        if [ -f "$bfile" ]; then
          IFS=' ' read -r count _ < "$bfile" || count=0
        fi
        case "$count" in ''|*[!0-9]*) count=0 ;; esac
        count=$((count + 1))
        tmp="$bfile.$$"
        # ponytail: atomic replace without fsync — the journal owns crash
        # durability; the breaker only needs to never be torn.
        printf '%s %s\n' "$count" "$class" > "$tmp" && mv -f -- "$tmp" "$bfile" \
          || { echo "QUEUE-ERROR:store"; exit 70; }
        if [ "$count" -ge 2 ]; then
          echo "QUEUE-ABORTED:systemic:$class"; exit 5
        fi
        echo "RECORDED:systemic:$class"; exit 0 ;;
      local|local:ci-timeout|success)
        tmp="$bfile.$$"
        printf '0 -\n' > "$tmp" && mv -f -- "$tmp" "$bfile" \
          || { echo "QUEUE-ERROR:store"; exit 70; }
        echo "RECORDED:reset"; exit 0 ;;
      *) usage ;;
    esac ;;
  note-failure)
    # REQ-205: normalize exactly <gate>|<first stderr line>, stripping digits
    # and path variability, then delegate ALL failure history to the existing
    # gates.py note-failure authority — never a second history store.
    [ -n "$queue_id" ] && [ -n "$item" ] && [ -n "$gate" ] || usage
    [ -n "$errfile" ] && [ -r "$errfile" ] || usage
    line="$(head -n 1 -- "$errfile" | head -c 512 | tr -d '\000')"
    normsig="$(printf '%s' "$line" \
      | sed -E 's#[^[:space:]]*/[^[:space:]]*# #g; s/[0-9]+//g; s/[[:space:]]+/ /g; s/^ //; s/ $//')"
    out="$(python3 "$GATES" note-failure "queue:${queue_id}:${item}" --sig "${gate}|${normsig}" 2>&1)"
    nrc=$?
    if [ "$nrc" -eq 75 ]; then
      # GATES-STORE-ERROR passes through verbatim for the classifier.
      printf '%s\n' "$out" >&2
      exit 75
    fi
    if [ "$nrc" -eq 1 ]; then echo "BLOCKED:no-progress"; exit 6; fi
    if [ "$nrc" -eq 0 ]; then echo "PROGRESS-OK"; exit 0; fi
    printf '%s\n' "$out" >&2
    echo "QUEUE-ERROR:store"; exit 70 ;;
  *) usage ;;
esac
