#!/usr/bin/env bash
# Evaluate synthetic/live vendor limit banners. The regex + fixtures are the drift-repair surface.
# Shape attributed to terryso/claude-auto-resume prior art; no vendor text is asserted as canonical.
set -uo pipefail

usage() { cat <<'USAGE'
Usage:
  session-wake.sh evaluate <capture-file> <drive-rc>
  session-wake.sh checkpoint <capture-file> <drive-rc> --run-id ID [--resume-argv argv ...]
USAGE
}
# $1 = typed token only; $2 = exit code. "$*" would fold the exit code into
# the typed message ("SESSION-WAKE:wake-exhausted 1"), breaking token matchers.
fail() { printf 'SESSION-WAKE:%s\n' "$1" >&2; exit "${2:-1}"; }
SESSION_WAKE_BANNER_RE='(limit reached|hit your limit|usage limit)[^[:cntrl:]]*(resets?|reset)'
FFS_SESSION_WAKE_MAX_SECS="${FFS_SESSION_WAKE_MAX_SECS:-21600}"
FFS_SESSION_WAKE_MAX_ATTEMPTS="${FFS_SESSION_WAKE_MAX_ATTEMPTS:-4}"
# lifecycle.sh lives beside this script — resolving it through the caller's
# git toplevel breaks when the run worktree is not the FFS checkout.
LIFECYCLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lifecycle.sh"

evaluate() {
  local capture="$1" drive_rc="$2" tail banner token epoch
  [ "$drive_rc" = 0 ] && return 0
  [ -s "$capture" ] || { printf 'SESSION-WAKE:no-banner WARN\n'; return 0; }
  tail="$(tail -n 50 "$capture")"
  banner="$(printf '%s\n' "$tail" | grep -E -i "$SESSION_WAKE_BANNER_RE" | tail -n 1 || true)"
  [ -n "$banner" ] || { printf 'SESSION-WAKE:no-banner WARN\n'; return 0; }
  token="$(printf '%s\n' "$banner" | sed -E -n 's/.*([0-9]{1,2}:[0-9]{2}([[:space:]]*([aApP][mM]))?([[:space:]]+[A-Za-z]{2,5})?).*/\1/p')"
  [[ "$token" =~ ^([01]?[0-9]|2[0-3]):[0-5][0-9]([[:space:]]*([aApP][mM]))?([[:space:]]+[A-Za-z]{2,5})?$ ]] || fail unparseable
  epoch="$(python3 - "$token" "$FFS_SESSION_WAKE_MAX_SECS" <<'PYEOF'
import datetime as dt, re, sys, time
raw, cap = sys.argv[1], int(sys.argv[2])
match = re.fullmatch(r'(\d{1,2}):(\d{2})(?:\s*([aApP][mM]))?(?:\s+[A-Za-z]{2,5})?', raw)
if not match: raise SystemExit(1)
h, minute, meridiem = int(match[1]), int(match[2]), match[3]
if meridiem:
    if not 1 <= h <= 12: raise SystemExit(1)
    h = h % 12 + (12 if meridiem.lower() == 'pm' else 0)
now = dt.datetime.now(); target = now.replace(hour=h, minute=minute, second=0, microsecond=0)
if target < now: target += dt.timedelta(days=1)
print(min(int(target.timestamp()), int(time.time()) + cap))
PYEOF
)" || fail unparseable
  printf 'SESSION-WAKE:wake-at:%s\n' "$epoch"
}

verb="${1:-}"; shift || true
case "$verb" in
  evaluate) [ "$#" -eq 2 ] || { usage; fail usage; }; evaluate "$1" "$2" ;;
  checkpoint)
    [ "$#" -ge 4 ] || { usage; fail usage; }; capture="$1"; rc="$2"; shift 2
    [ "$1" = --run-id ] || { usage; fail usage; }; run_id="$2"; shift 2
    resume='["scripts/gsd/gsd-run.sh"]'
    if [ "${1:-}" = --resume-argv ]; then shift; resume="$(python3 - "$@" <<'PYEOF'
import json, sys
print(json.dumps(sys.argv[1:]))
PYEOF
)"; fi
    result="$(evaluate "$capture" "$rc")"; status=$?
    [ "$status" -eq 0 ] || exit "$status"
    [ -n "$result" ] || exit 0
    printf '%s\n' "$result"
    [[ "$result" == SESSION-WAKE:wake-at:* ]] || exit 0
    wake_at="${result##*:}"
    bash "$LIFECYCLE" wake-checkpoint "$run_id" "$wake_at" "$resume" "$FFS_SESSION_WAKE_MAX_ATTEMPTS" || {
      [ "$?" -eq 2 ] && fail wake-exhausted 1; fail lifecycle; }
    ;;
  *) usage; fail usage ;;
esac
