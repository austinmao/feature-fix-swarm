#!/usr/bin/env bash
# cloud-dispatch.sh - thin wrapper for lib/cloud_dispatch.py (contract section 3).
# Library lookup: $FFS_LIB_DIR, then <script>/../../lib, then the managed lib root.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR=""
for candidate in "${FFS_LIB_DIR:-}" "$SCRIPT_DIR/../../lib" "$HOME/.claude/lib/feature-fix-swarm"; do
  if [ -n "$candidate" ] && [ -f "$candidate/cloud_dispatch.py" ]; then
    LIB_DIR="$candidate"
    break
  fi
done
if [ -z "$LIB_DIR" ]; then
  echo "cloud-dispatch.sh: cloud_dispatch.py not found (set FFS_LIB_DIR)" >&2
  exit 78
fi
exec python3 "$LIB_DIR/cloud_dispatch.py" "$@"
