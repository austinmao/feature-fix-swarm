#!/usr/bin/env bash
# seed-ceremony-tier.sh — seed-time ceremony classifier (D3, operator
# decision 2026-08-27): pick how much review ceremony a spec earns BEFORE
# the run pays for it. Advisory — always exit 0; consumers (spec-decompose)
# act on the printed tier.
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
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/gsd/security-surface.sh
. "$SCRIPT_DIR/security-surface.sh"

case "${FFS_CEREMONY_TIER:-}" in
  full|light|adhoc)
    echo "$FFS_CEREMONY_TIER override FFS_CEREMONY_TIER"
    exit 0
    ;;
esac

SPEC="${1:-}"
PLAN="${2:-}"
EST_FILES="${3:-}"
EST_LOC="${4:-}"
case "$EST_FILES" in *[!0-9]*|'') EST_FILES="" ;; esac
case "$EST_LOC" in *[!0-9]*|'') EST_LOC="" ;; esac

# security surface: grep spec + plan content against the single-home keyword
# list (same pattern review-tier.sh applies to paths at diff time)
for f in "$SPEC" "$PLAN"; do
  [ -n "$f" ] && [ -f "$f" ] || continue
  if grep -qiE "$KEYWORDS" "$f"; then
    echo "full security-surface"
    exit 0
  fi
done

# size ladder (thresholds mirror review-tier.sh's diff ladder, scaled to
# seed-time estimates: >20 files or >1500 est-LOC is full-size work)
if { [ -n "$EST_FILES" ] && [ "$EST_FILES" -gt 20 ]; } || \
   { [ -n "$EST_LOC" ] && [ "$EST_LOC" -gt 1500 ]; }; then
  echo "full size"
  exit 0
fi
if [ -n "$EST_FILES" ] && [ "$EST_FILES" -lt 5 ] && \
   [ -n "$EST_LOC" ] && [ "$EST_LOC" -lt 200 ]; then
  echo "adhoc small"
  exit 0
fi
echo "light default"
exit 0
