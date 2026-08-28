#!/usr/bin/env bash
# sync-drift-check.sh — vendor-drift detector for the packaged gsd levers
# (borrowed: buildomator's check-drift ratchet pattern; generalizes the
# FALLBACK-017 single-file check to the whole shell/Python scripts/gsd surface).
#
# A consumer repo (e.g. a monorepo vendoring feature-fix-swarm) carries LIVE
# COPIES of these levers that drift independently — measured 2026-07-31: six
# drifted files, one of them a deliberate fork a blind sync would have
# destroyed. This makes the drift machine-visible and the forks auditable:
#
#   identical                  -> IN-SYNC
#   absent in consumer         -> MISSING (warn only — partial installs are legal)
#   differs, in allowlist      -> FORKED (info, reason printed)
#   differs, NOT in allowlist  -> DRIFT (exit 1)
#   in allowlist but identical -> STALE-ALLOWLIST (warn — prune the entry)
#
# Usage: sync-drift-check.sh <consumer-scripts-dir> [--allowlist FILE]
#   Allowlist format, one per line:  <filename> <free-text reason>
#   GSD_SYNC_SRC  override the packaged-lever dir (tests; default: this dir)
#
# Run from the consumer side after every vendor sync, or wire into consumer CI.
set -euo pipefail

SRC="${GSD_SYNC_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)}"
CONSUMER="${1:-}"
ALLOWLIST=""
[ "${2:-}" = "--allowlist" ] && ALLOWLIST="${3:-}"

if [ -z "$CONSUMER" ] || [ ! -d "$CONSUMER" ]; then
  echo "[sync-drift-check] ERROR: consumer scripts dir '$CONSUMER' not found" >&2
  echo "usage: sync-drift-check.sh <consumer-scripts-dir> [--allowlist FILE]" >&2
  exit 2
fi

# Self-compare guard (spec-012): comparing a dir to itself prints IN-SYNC for
# every file and STALE-ALLOWLIST for every real fork — a false green on the
# exact question this tool answers. Realpath both sides so symlink aliases
# and ./ or trailing-slash spellings collapse (AC-003). A SRC that does not
# exist falls through to today's behaviour (EDGE-001). The `-ef` device+inode
# check (wall residual e62537db6216) additionally catches the same directory
# exposed through two distinct Linux bind-mount paths, where the two `pwd -P`
# strings differ even though both name the same inode.
_src_real="$(cd "$SRC" 2>/dev/null && pwd -P || printf '%s' "$SRC")"
_dst_real="$(cd "$CONSUMER" && pwd -P)"
if [ "$_src_real" = "$_dst_real" ] || [ "$_src_real" -ef "$_dst_real" ]; then
  echo "[sync-drift-check] ERROR: SELF-COMPARE — source and consumer are the same directory ($_dst_real); every file would report IN-SYNC" >&2
  echo "usage: GSD_SYNC_SRC=<packaged scripts/gsd dir> sync-drift-check.sh <consumer-scripts-dir> [--allowlist FILE]" >&2
  exit 2
fi

allow_reason() {
  # Prints the reason when $1 is allowlisted, else nothing.
  [ -n "$ALLOWLIST" ] && [ -f "$ALLOWLIST" ] || return 0
  awk -v f="$1" '$1 == f { $1=""; sub(/^ /,""); print; exit }' "$ALLOWLIST"
}

drift=0
# Python helpers are part of the runnable lever surface too (the isolated
# Codex runner delegates bundle, grant, hash, and config work to them).
for src_file in "$SRC"/*.sh "$SRC"/*.py; do
  [ -f "$src_file" ] || continue
  name="$(basename "$src_file")"
  dst="$CONSUMER/$name"
  reason="$(allow_reason "$name")"
  if [ ! -f "$dst" ]; then
    echo "MISSING: $name (no consumer copy — legal for partial installs)"
  elif cmp -s "$src_file" "$dst"; then
    if [ -n "$reason" ]; then
      echo "STALE-ALLOWLIST: $name is identical — prune its allowlist entry"
    else
      echo "IN-SYNC: $name"
    fi
  elif [ -n "$reason" ]; then
    echo "FORKED: $name ($reason)"
  else
    echo "DRIFT: $name differs and is not an allowlisted fork"
    drift=$((drift + 1))
  fi
done

if [ "$drift" -gt 0 ]; then
  echo "[sync-drift-check] FAIL: $drift unlisted drifted file(s) — sync them or record the fork in the allowlist"
  exit 1
fi
echo "[sync-drift-check] OK"
