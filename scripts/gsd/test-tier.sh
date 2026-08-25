#!/usr/bin/env bash
# test-tier.sh — the single source of CI test commands (spec-007 REQ-303).
# Rendered workflows never hardcode a test command; they run
#   scripts/gsd/test-tier.sh <tier> > "$RUNNER_TEMP/tier-cmds" && bash -e ...
# so a lookup failure fails the job DISTINCTLY from a test failure.
#
# Usage: test-tier.sh <fast|full|nightly|live> [registry]
#   registry defaults to $ROOT/config/environments.yaml
#
# stdout: ONLY the registered command line(s), one per line, declaration
#         order (EDGE-006: duplicate tier rows all print, in order).
# stderr: usage and all diagnostics — never stdout.
# exit:   0 lookup ok (a KNOWN tier with zero rows prints nothing, exit 0 —
#           OQ6: a registry legitimately defines only the tiers it has);
#         2 usage error / unknown tier token;
#         3 registry missing or unreadable + one-line reason.
#
# Mirrors review-tier.sh's idiom (set -uo pipefail, usage→stderr exit 2,
# while/case argv, ROOT from BASH_SOURCE) but deliberately NOT its
# fail-safe-exit-0 (RESEARCH Pitfall 3): a missing registry must fail the
# CI job, never silently run nothing (threat T-03-11).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
  echo "Usage: test-tier.sh <fast|full|nightly|live> [registry]" >&2
  exit 2
}

TIER=""
REGISTRY=""
POS=0

while [ $# -gt 0 ]; do
  case "$POS" in
    0) TIER="$1" ;;
    1) REGISTRY="$1" ;;
    *) usage ;;
  esac
  POS=$((POS + 1))
  shift
done

[ -n "$TIER" ] || usage
case "$TIER" in
  fast|full|nightly|live) ;;
  *) usage ;;
esac

# arg-2 override honored before any default resolution.
[ -n "$REGISTRY" ] || REGISTRY="$ROOT/config/environments.yaml"

if [ ! -f "$REGISTRY" ] || [ ! -r "$REGISTRY" ]; then
  echo "[test-tier] registry not readable at ${REGISTRY##*/} — remedy: run" \
    "'env-registry.sh detect' then 'apply' to create it, or pass [registry]" >&2
  exit 3
fi

# Flat-scalar lookup over the test_tiers: rows — no new parser; comment strip
# matches _manifest_scalar semantics (whitespace-then-# split).
# Wall c26a40e9 (block-boundary latch): the scan LATCHES on the indent-0
# `test_tiers:` line and TERMINATES at the next non-blank non-comment
# indent-0 key, so tier/command-shaped rows inside environments: or
# surfaces: can never leak into lookup.
awk -v tier="$TIER" '
  /^[ \t]*#/ { next }                       # comments never affect the latch
  /^[^ \t]/ {                               # non-blank indent-0 key
    inblock = ($0 ~ /^test_tiers:[ \t]*$/)
    cur = ""
    next
  }
  !inblock { next }
  {
    line = $0
    sub(/^[ \t]+/, "", line)
    if (line ~ /^- tier:/) {
      val = line
      sub(/^- tier:[ \t]*/, "", val)
      sub(/[ \t]+#.*$/, "", val)
      cur = val
    } else if (line ~ /^command:/ && cur == tier) {
      val = line
      sub(/^command:[ \t]*/, "", val)
      sub(/[ \t]+#.*$/, "", val)
      print val
    }
  }
' "$REGISTRY"
