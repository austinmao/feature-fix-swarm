#!/usr/bin/env bash
# waiver-record.sh — single authoritative shell adapter for bypass evidence.
set -uo pipefail

if [ "$#" -ne 2 ]; then
  echo "WAIVER-UNRECORDED: usage: waiver-record.sh <gate> <env-var>" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
COMMON_DIR="$(git -C "$SCRIPT_DIR" rev-parse --git-common-dir 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ] || [ -z "$COMMON_DIR" ] || [ ! -f "$REPO_ROOT/lib/gates.py" ]; then
  echo "WAIVER-UNRECORDED: STORE-AUTHORITY" >&2
  exit 1
fi

# Run from the recorder's repository with caller-controlled Git/store variables
# removed. gates.py then resolves the same canonical common-dir evidence store
# that every linked worktree shares.
if ! (cd "$REPO_ROOT" && env -u GATES_STORE -u GIT_DIR -u GIT_COMMON_DIR -u GIT_WORK_TREE \
  python3 "$REPO_ROOT/lib/gates.py" waiver --run-id "${GSD_RUN_ID:-unattributed}" \
  --gate "$1" --env-var "$2"); then
  echo "WAIVER-UNRECORDED: WRITE-FAILED" >&2
  exit 1
fi
