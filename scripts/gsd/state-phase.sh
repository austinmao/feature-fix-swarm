#!/usr/bin/env bash
# Prints the BODY-derived completed-phases integer from a gsd STATE.md file.
# Frontmatter counters (completed_phases/percent) are documented-unreliable
# upstream (gsd-core 1.6.1 issues #1514/#1446/#1264/#274/#2012/#2022) — this
# script strips frontmatter before parsing so it can never read those values.
# Usage: state-phase.sh [state-file]   (default: .planning/STATE.md)
set -uo pipefail

if [ $# -gt 1 ]; then
  echo "usage: state-phase.sh [state-file]" >&2
  exit 2
fi

STATE_FILE="${1:-.planning/STATE.md}"
if [ ! -f "$STATE_FILE" ]; then
  echo "state-phase: $STATE_FILE not found" >&2
  exit 2
fi

BODY="$(awk '
  BEGIN{fm=0}
  NR==1 && $0=="---"{fm=1; next}
  fm==1 && $0=="---"{fm=2; next}
  fm!=1{print}
' "$STATE_FILE")"

PHASE_LINE="$(printf '%s\n' "$BODY" | grep -E '^Phase:[[:space:]]*[0-9]+[[:space:]]+of[[:space:]]+[0-9]+' | head -1)"
if [ -z "$PHASE_LINE" ]; then
  echo "state-phase: no 'Phase: X of Y' line found in $STATE_FILE body" >&2
  exit 1
fi
CURRENT="$(printf '%s\n' "$PHASE_LINE" | sed -E 's/^Phase:[[:space:]]*([0-9]+).*/\1/')"

STATUS_LINE="$(printf '%s\n' "$BODY" | grep -E '^Status:' | head -1)"
if printf '%s' "$STATUS_LINE" | grep -qi 'phase complete'; then
  COMPLETED="$CURRENT"
else
  COMPLETED=$((CURRENT - 1))
  [ "$COMPLETED" -lt 0 ] && COMPLETED=0
fi

printf '%s\n' "$COMPLETED"
exit 0
