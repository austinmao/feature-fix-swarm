#!/usr/bin/env bash
# FFS cloud SessionStart hook. Acts only when CLAUDE_CODE_REMOTE=true AND
# (branch matches ffs/cloud/* OR FFS_CLOUD_PROVISION=1). Always exits 0 and
# prints nothing. bash 3.2 safe.
set -u

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

# Resolve the repo root without depending on the working directory: the project
# dir Claude Code exports, else the git toplevel of THIS script's own directory,
# else two levels above it. The setup script always comes from that root, so a
# copy of this hook planted in a subdirectory cannot redirect it.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)" || exit 0
ROOT=""
if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "$CLAUDE_PROJECT_DIR" ]; then
  ROOT="$(cd -- "$CLAUDE_PROJECT_DIR" && pwd -P)" || ROOT=""
fi
[ -n "$ROOT" ] || ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" || ROOT=""
[ -n "$ROOT" ] || ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)" || exit 0

# Detached HEAD yields an empty branch name, which never matches.
BRANCH="$(git -C "$ROOT" symbolic-ref --short -q HEAD 2>/dev/null || true)"
case "$BRANCH" in
  ffs/cloud/*) ;;
  *) [ "${FFS_CLOUD_PROVISION:-}" = "1" ] || exit 0 ;;
esac

SETUP="$ROOT/.ffs/cloud/setup.sh"
[ -f "$SETUP" ] || exit 0
CAP="${FFS_CLOUD_SETUP_TIMEOUT:-240}"
case "$CAP" in '' | *[!0-9]*) CAP=240 ;; esac
LOG="$HOME/.ffs-cloud-setup.log"
STATUS_FILE="$HOME/.ffs-cloud-env-status"
umask 077

rc=0
if command -v timeout >/dev/null 2>&1; then
  (cd "$ROOT" && timeout "$CAP" bash "$SETUP") >"$LOG" 2>&1 </dev/null || rc=$?
elif command -v perl >/dev/null 2>&1; then
  (cd "$ROOT" && perl -e 'alarm shift; exec @ARGV' "$CAP" bash "$SETUP") >"$LOG" 2>&1 </dev/null || rc=$?
else
  (cd "$ROOT" && exec bash "$SETUP") >"$LOG" 2>&1 </dev/null &
  pid=$!
  (sleep "$CAP"; kill -TERM "$pid" 2>/dev/null; sleep 2; kill -KILL "$pid" 2>/dev/null) >/dev/null 2>&1 &
  watcher=$!
  wait "$pid" || rc=$?
  kill "$watcher" 2>/dev/null
fi

# setup.sh writes the real status; record a stub only when it never got to.
if [ "$rc" -ne 0 ] && [ ! -f "$STATUS_FILE" ]; then
  printf '{"schema":1,"ok":false,"session_start":"setup-failed","rc":%s}\n' "$rc" >"$STATUS_FILE" 2>/dev/null
fi
exit 0
