#!/usr/bin/env bash
# liveness-check.sh — composite AND-of-death autonomous-run liveness detector.
#
# adopt-wip needs a resilient composite check before abandoning WIP: FFS today
# declares runs dead on single-signal probes, but one transient failed probe
# (a stale pidfile check, a mtime race) must never kill an overnight wave
# (US8). This script reports ALIVE (exit 0) if ANY of three signals holds,
# and DEAD (exit 1) ONLY when ALL THREE fail (AC-011, PATH-007):
#
#   P (pid)   pidfile holds a positive integer and `kill -0 <pid>` succeeds
#   M (mtime) newest mtime under <state-dir> is within --window-min of now
#   G (grant) gates.py check-grant <run-id> --action ship:gsd returns rc 0
#
#   ALIVE iff P || M || G ; DEAD (exit 1) iff !P && !M && !G
#
# Usage: liveness-check.sh <pidfile> <state-dir> [--run-id ID] [--window-min N]
#   --run-id ID        explicit run id for the grant signal (else GSD_RUN_ID,
#                       else derived from the current branch's spec-NNN prefix)
#   --window-min N      mtime freshness window in minutes (default: env
#                       LIVENESS_WINDOW_MIN, else 30)
#
# Self-contained contract (RESOLVED Open Question 3): the repo's lib/run_state/
# is an unrelated SQLite CLI with no pid column, so this script does NOT
# consume it — it invents its own pidfile + state-dir inputs. The ship-grant
# signal reuses the exact GATES_PY resolution + RUN_ID derivation pattern from
# review-gate-command.sh so both levers agree on where the grant ledger lives.
#
# Documented accepted risk (EDGE-005): a PID reused by an unrelated process
# after the original run died will read as P=alive. The mtime and grant
# signals bound this exposure to a window-bounded false-alive, not an
# unbounded one.
set -uo pipefail

if [ $# -lt 2 ]; then
  echo "usage: liveness-check.sh <pidfile> <state-dir> [--run-id ID] [--window-min N]" >&2
  exit 2
fi

PIDFILE="$1"; shift
STATE_DIR="$1"; shift
RUN_ID_ARG=""
WINDOW_MIN="${LIVENESS_WINDOW_MIN:-30}"

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)
      if [ $# -lt 2 ]; then
        echo "usage: liveness-check.sh <pidfile> <state-dir> [--run-id ID] [--window-min N]" >&2
        exit 2
      fi
      RUN_ID_ARG="$2"; shift 2 ;;
    --window-min)
      if [ $# -lt 2 ]; then
        echo "usage: liveness-check.sh <pidfile> <state-dir> [--run-id ID] [--window-min N]" >&2
        exit 2
      fi
      WINDOW_MIN="$2"; shift 2 ;;
    *)
      echo "usage: liveness-check.sh <pidfile> <state-dir> [--run-id ID] [--window-min N]" >&2
      exit 2 ;;
  esac
done

# ── Signal P: pidfile → live process ─────────────────────────────────────
p_signal="dead"
if [ -f "$PIDFILE" ]; then
  pid_raw="$(tr -d '[:space:]' < "$PIDFILE" 2>/dev/null || true)"
  if [[ "$pid_raw" =~ ^[0-9]+$ ]] && [ "$pid_raw" -gt 0 ] 2>/dev/null; then
    if kill -0 "$pid_raw" 2>/dev/null; then
      p_signal="alive"
    fi
  fi
fi

# ── Signal M: newest mtime under state-dir within window ────────────────
# Portable (bash-3.2-safe, no mapfile/declare -A): enumerate files with a
# plain `find` (no timestamp flags — findutils/BSD find/bfs all agree on
# that), then take the max mtime via whichever `stat` dialect exists.
# GNU `-c %Y` is tried FIRST: GNU stat's `-f` is filesystem-info mode (not
# "use this format string"), so on Linux `stat -f %m <file>` doesn't error —
# it silently prints the mount point instead of the mtime, so the `||`
# fallback to `-c %Y` never fires and the signal reads as permanently stale.
# BSD/macOS stat has no `-c` flag, so it errors there and correctly falls
# through to `-f %m`.
_stat_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || true
}

m_signal="stale"
if [ -d "$STATE_DIR" ]; then
  newest=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    m="$(_stat_mtime "$f")"
    [ -n "$m" ] && [ "$m" -gt "$newest" ] 2>/dev/null && newest="$m"
  done < <(find "$STATE_DIR" -type f 2>/dev/null)
  if [ "$newest" -gt 0 ]; then
    now="$(date +%s)"
    age=$(( now - newest ))
    window_sec=$(( WINDOW_MIN * 60 ))
    # age<0 = future-dated file (clock skew/corrupt) — do not trust as fresh
    if [ "$age" -ge 0 ] && [ "$age" -le "$window_sec" ]; then
      m_signal="fresh"
    fi
  fi
fi

# ── Signal G: ship:gsd grant in flight (gates.py check-grant) ───────────
# GATES_PY 3-candidate resolution + RUN_ID derivation copied verbatim from
# review-gate-command.sh so both levers agree on where the ledger lives.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
RUN_ID="$RUN_ID_ARG"
[ -z "$RUN_ID" ] && RUN_ID="${GSD_RUN_ID:-}"
if [ -z "$RUN_ID" ]; then
  BRANCH_NNN="$(git branch --show-current 2>/dev/null | grep -oE '^[0-9]{3}' | head -1)"
  [ -n "$BRANCH_NNN" ] && RUN_ID="spec-${BRANCH_NNN}"
fi

GATES_PY=""
for candidate in \
  "$REPO_ROOT/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$REPO_ROOT/lib/gates.py"; do
  [ -f "$candidate" ] && GATES_PY="$candidate" && break
done

g_signal="not-granted"
if [ -n "$GATES_PY" ] && [ -n "$RUN_ID" ]; then
  # Sanitize before it is ever used as a ledger key/path component (T-01-LV-01).
  safe_run_id="$(printf '%s' "$RUN_ID" | tr -c 'A-Za-z0-9._-' '_')"
  if python3 "$GATES_PY" check-grant "$safe_run_id" --action "ship:gsd" >/dev/null 2>&1; then
    g_signal="granted"
  fi
fi

echo "liveness-check: P=${p_signal}"
echo "liveness-check: M=${m_signal}"
echo "liveness-check: G=${g_signal}"

if [ "$p_signal" = "alive" ] || [ "$m_signal" = "fresh" ] || [ "$g_signal" = "granted" ]; then
  echo "liveness-check: ALIVE (exit 0)"
  exit 0
fi

echo "liveness-check: DEAD (exit 1)"
exit 1
