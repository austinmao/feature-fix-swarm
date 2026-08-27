#!/usr/bin/env bash
# seed-ceremony-tier.sh — seed-time ceremony classifier (D3, operator
# decision 2026-08-27): pick how much review ceremony a spec earns BEFORE
# the run pays for it.
#
# Usage: seed-ceremony-tier.sh <spec.md> [plan.md] [est-files] [est-loc]
#   est-files/est-loc: spec-decompose's own estimates (integers; omitted or
#   non-numeric -> unknown, which never triggers the small/adhoc rungs).
#
# Output (stdout, single line): "<tier> <reason>"
#   full   — security-surface keyword match, or files>20 / est-LOC>1500
#   adhoc  — files<5 AND est-LOC<200 (recommend /feature-implement --adhoc)
#   light  — everything else (2-phase cap + one run-level wall)
#   FFS_CEREMONY_TIER=full|light|adhoc is a hard override.
#
# ADVISORY, never a gate: this script classifies, it does not block. That is
# structural here — there is exactly ONE exit point (the final echo), so the
# status is always the echo's. Callers act on the printed tier, never on rc.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/gsd/security-surface.sh
. "$SCRIPT_DIR/security-surface.sh"

SPEC="${1:-}"
PLAN="${2:-}"
EST_FILES="${3:-}"
EST_LOC="${4:-}"
case "$EST_FILES" in *[!0-9]*|'') EST_FILES="" ;; esac
case "$EST_LOC" in *[!0-9]*|'') EST_LOC="" ;; esac

TIER=""
REASON=""

case "${FFS_CEREMONY_TIER:-}" in
  full|light|adhoc) TIER="$FFS_CEREMONY_TIER"; REASON="override FFS_CEREMONY_TIER" ;;
esac

# security surface: grep spec + plan content against the single-home keyword
# list (same pattern review-tier.sh applies to paths at diff time)
if [ -z "$TIER" ]; then
  for f in "$SPEC" "$PLAN"; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    if grep -qiE "$KEYWORDS" "$f"; then
      TIER=full; REASON="security-surface"
      break
    fi
  done
fi

# size ladder (thresholds mirror review-tier.sh's diff ladder, scaled to
# seed-time estimates: >20 files or >1500 est-LOC is full-size work)
if [ -z "$TIER" ]; then
  if { [ -n "$EST_FILES" ] && [ "$EST_FILES" -gt 20 ]; } || \
     { [ -n "$EST_LOC" ] && [ "$EST_LOC" -gt 1500 ]; }; then
    TIER=full; REASON=size
  elif [ -n "$EST_FILES" ] && [ "$EST_FILES" -lt 5 ] && \
       [ -n "$EST_LOC" ] && [ "$EST_LOC" -lt 200 ]; then
    TIER=adhoc; REASON=small
  else
    TIER=light; REASON=default
  fi
fi

echo "$TIER $REASON"
