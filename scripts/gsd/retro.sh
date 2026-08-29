#!/usr/bin/env bash
# retro.sh — local-only FFS diagnostic collection and analysis boundary.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RETRO_PY="$SCRIPT_DIR/../../lib/retro_scrub.py"
STATE_PY="$SCRIPT_DIR/../../lib/retro_state.py"
SCANNER="$SCRIPT_DIR/scan-handoff-credentials.sh"
GUARD="$SCRIPT_DIR/../hooks/credential-output-guard.sh"

usage() {
  echo "usage: retro.sh collect|analyze|check-consent|consent [--digest PATH] [--findings PATH] [--changelog PATH] [--state-root PATH]" >&2
}

mode="${1:-}"
case "$mode" in
  check-consent) [ "$#" -eq 1 ] || { usage; exit 2; }; exec python3 "$STATE_PY" check-consent ;;
  consent) shift; [ -f "$STATE_PY" ] || { echo "RETRO:missing-state" >&2; exit 1; }; exec python3 "$STATE_PY" consent "${1:-}" ;;
  collect|analyze) shift ;;
  *) usage; exit 2 ;;
esac

digest=""
findings=""
changelog=""
state_root=""
no_retro=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-retro) no_retro=1; shift ;;
    --digest|--findings|--changelog|--state-root)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      case "$1" in
        --digest) digest="$2" ;;
        --findings) findings="$2" ;;
        --changelog) changelog="$2" ;;
        --state-root) state_root="$2" ;;
      esac
      shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

# These are deliberately the first analyze decisions: no repository/input or
# user-state access occurs on either disabled route.
if [ "$mode" = "analyze" ] && { [ "${FFS_RETRO:-on}" = "off" ] || [ "$no_retro" -eq 1 ]; }; then
  echo "RETRO:disabled"
  exit 0
fi

if [ -n "$digest$findings$changelog$state_root" ] && [ "${RETRO_TEST_SEAM:-}" != "1" ]; then
  echo "RETRO:seam-rejected" >&2
  exit 1
fi

[ -f "$RETRO_PY" ] || { echo "RETRO:missing-module" >&2; exit 1; }

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$repo_root" ] || { echo "RETRO:no-repository" >&2; exit 1; }
# auto_discovered marks a digest this script found itself (oldest-first, so
# an older unconsumed backlog file is never shadowed by a newer one). An
# explicit --digest is a test seam pointing at a checked-in fixture and is
# never renamed.
auto_discovered=0
if [ -z "$digest" ]; then
  digest="$(find "$repo_root/.feature-fix-swarm" -maxdepth 1 -type f -name 'digest-*.jsonl' -print 2>/dev/null | sort | head -n 1)"
  [ -n "$digest" ] && auto_discovered=1
fi
[ -n "$digest" ] || { echo "RETRO:no-events"; exit 0; }
[ -n "$findings" ] || findings="$repo_root/.feature-fix-swarm/findings.json"
[ -n "$changelog" ] || changelog="$repo_root/CHANGELOG.md"
[ -n "$state_root" ] || state_root="${HOME}/.cache/feature-fix-swarm"

if [ "$mode" = "collect" ]; then
  exec python3 "$RETRO_PY" collect --digest "$digest" --findings "$findings" --changelog "$changelog"
fi

# Snapshot-first claim: rename an auto-discovered digest to .processing
# BEFORE any read, not after analysis. A producer append that lands after
# this mv starts a fresh digest-*.jsonl -- no event is ever silently folded
# into a consumed file. Every later outcome (parse failure, filing failure,
# a crash anywhere in between) leaves the file out of the *.jsonl glob, so
# it is never re-read and never double-filed; bytes are always preserved.
if [ "$auto_discovered" -eq 1 ]; then
  claimed="$digest.processing"
  if ! mv "$digest" "$claimed"; then
    echo "RETRO:snapshot-failed" >&2
    exit 1
  fi
  digest="$claimed"
fi

[ -f "$STATE_PY" ] || { echo "RETRO:missing-state" >&2; exit 1; }

# The legacy Phase 1 explicit state-root seam is intentionally scrub-only.
# Production consent and filing state are never redirected from HOME.
fixed_root="$HOME/.cache/feature-fix-swarm"
if [ "${RETRO_TEST_SEAM:-}" != "1" ] || [ "$state_root" = "$fixed_root" ]; then
  consent_state="$(python3 "$STATE_PY" check-consent)"
  if [ "$consent_state" != "RETRO:consent-granted" ]; then
    echo "RETRO:no-consent"
    exit 0
  fi
fi

if [ ! -x "$SCANNER" ] || [ ! -x "$GUARD" ]; then
  echo "RETRO:scanner-unavailable" >&2
  exit 1
fi
handoff="$(mktemp "${TMPDIR:-/tmp}/ffs-retro-handoff.XXXXXX")" || { echo "RETRO:local-error" >&2; exit 1; }
chmod 600 "$handoff"
trap 'rm -f "$handoff"' EXIT
if ! RETRO_FILING=1 python3 "$RETRO_PY" analyze --digest "$digest" --findings "$findings" --changelog "$changelog" --state-root "$state_root" --scanner "$SCANNER" > "$handoff"; then
  # An unparseable digest would otherwise sit at the head of the oldest-first
  # queue forever and block every newer file behind it. It is already
  # claimed (.processing, out of the *.jsonl glob); move it to .rejected
  # intact (never delete) so the next run can drain past it.
  if [ "$auto_discovered" -eq 1 ]; then
    mv -f "$digest" "${digest%.processing}.rejected" 2>/dev/null || true
  fi
  exit 1
fi

if [ "${RETRO_TEST_SEAM:-}" != "1" ] || [ "$state_root" = "$fixed_root" ]; then
  if ! gh auth status; then
    python3 "$STATE_PY" record-auth-failure || true
    cat "$handoff"
    exit 0
  fi
  python3 "$STATE_PY" file "$handoff"
  file_rc=$?
  if [ "$auto_discovered" -eq 1 ]; then
    if [ "$file_rc" -eq 0 ]; then
      # digest.sh flushes the current day's file (run-finalizer.sh calls it
      # immediately before retro), so consuming today's file here is safe --
      # the producer's next append starts a fresh file, never this one.
      if ! mv -f "$digest" "${digest%.processing}.consumed" 2>/dev/null; then
        echo "RETRO:consume-rename-failed $digest" >&2
      fi
    else
      # Filing failed (or a crash lands here): leave the claimed file at
      # .processing rather than renaming it. It stays out of the *.jsonl
      # glob (never auto-reprocessed, no duplicate writes) with its bytes
      # intact (no loss); an operator can rename it back to re-queue.
      echo "RETRO:filing-failed-digest-left-processing $digest" >&2
    fi
  fi
  cat "$handoff"
  exit "$file_rc"
fi
cat "$handoff"
