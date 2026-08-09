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
  echo "scan-handoff-credentials: WARN: credential guard absent; continuing" >&2
  exit 0
fi

"$GUARD" --scan-file "$1"
