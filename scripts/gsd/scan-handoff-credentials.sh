#!/usr/bin/env bash
# scan-handoff-credentials.sh — shared, quiet handoff-file credential gate.
set -uo pipefail

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "scan-handoff-credentials: usage: <regular-file>" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$SCRIPT_DIR/../hooks/credential-output-guard.sh"
if [ ! -x "$GUARD" ]; then
  # FAIL-CLOSED: exit 0 here would tell publish-scanned-handoff.sh the file
  # is clean when nothing looked at it. A missing guard is a broken install,
  # not a clean scan.
  echo "scan-handoff-credentials: BLOCKED — credential guard absent; nothing was scanned" >&2
  exit 3
fi

"$GUARD" --scan-file "$1"
