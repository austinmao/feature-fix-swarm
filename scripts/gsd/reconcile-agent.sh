#!/usr/bin/env bash
# Opt-in scheduler for reconcile.sh, outside every Claude Code process tree.
#
# reconcile.sh is a one-shot pass. Without a scheduler a run that died (memory
# reap, session end, SIGKILL) waits until an FFS entrypoint happens to be
# invoked again -- which, overnight, is never. This installs a per-user
# launchd agent (macOS) that runs one pass every FFS_RECONCILE_INTERVAL_SECS
# (300) from the consumer repo. launchd owns the process, so a reap or kill of
# any Claude session cannot stop it, and it survives logout/login.
#
#   reconcile-agent.sh install   [--repo PATH] [--interval SECS]
#   reconcile-agent.sh uninstall [--repo PATH]
#   reconcile-agent.sh status    [--repo PATH]        rc 0 installed+loaded, 1 otherwise
#
# Linux hosts get the equivalent cron line printed; this script never edits a
# crontab. docs/healing.md: FFS installs a scheduler ONLY through this explicit
# opt-in (or /feature-implement --autonomous asking once at plan approval).
set -uo pipefail

usage() { sed -n '/^#   reconcile-agent.sh install/,/^#   reconcile-agent.sh status/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,3\}//'; }
fail() { printf 'RECONCILE-AGENT:%s\n' "$*" >&2; exit 1; }
say() { printf 'RECONCILE-AGENT:%s\n' "$*"; }

verb="${1:-}"; shift || true
REPO=""; INTERVAL="${FFS_RECONCILE_INTERVAL_SECS:-300}"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift ;;
    --interval) INTERVAL="${2:-}"; shift ;;
    *) usage >&2; fail "usage unknown-arg $1" ;;
  esac; shift
done
[[ "$INTERVAL" =~ ^[0-9]+$ ]] && [ "$INTERVAL" -ge 60 ] || fail "invalid --interval (seconds, >= 60)"
[ -n "$REPO" ] || REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
REPO="$(cd "$REPO" 2>/dev/null && pwd -P)" || fail "repo not found"
RECONCILE="$REPO/scripts/gsd/reconcile.sh"
[ -f "$RECONCILE" ] || fail "missing $RECONCILE (run setup.sh --reconcile-consumer first)"

hash8() { printf '%s' "$1" | { shasum -a 256 2>/dev/null || sha256sum; } | cut -c1-8; }
LABEL="org.featurefixswarm.reconcile.$(hash8 "$REPO")"
AGENTS_DIR="${FFS_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$AGENTS_DIR/$LABEL.plist"
LOG_DIR="$REPO/.planning/run-state"
LOG="$LOG_DIR/reconcile-agent.log"
DOMAIN="gui/$(id -u)"
LAUNCHCTL="${FFS_LAUNCHCTL:-launchctl}"

is_macos() { [ "$(uname -s)" = Darwin ]; }
cron_line() { printf '*/%d * * * * cd %q && bash scripts/gsd/reconcile.sh >> %q 2>&1\n' "$(( INTERVAL / 60 ))" "$REPO" "$LOG"; }

write_plist() {
  mkdir -p "$AGENTS_DIR" "$LOG_DIR"
  # PATH is captured at install time: launchd agents get a minimal default
  # PATH, and the relaunched runner needs codex/claude/jq/python3/node.
  python3 - "$PLIST" "$LABEL" "$REPO" "$INTERVAL" "$LOG" "$PATH" <<'PY'
import plistlib, sys
path, label, repo, interval, log, env_path = sys.argv[1:]
plistlib.dump({
    "Label": label,
    "ProgramArguments": ["/bin/bash", "scripts/gsd/reconcile.sh"],
    "WorkingDirectory": repo,
    "StartInterval": int(interval),
    "RunAtLoad": True,
    "StandardOutPath": log,
    "StandardErrorPath": log,
    "EnvironmentVariables": {"PATH": env_path, "FFS_RECONCILE_AGENT": "1"},
    "ProcessType": "Background",
    "Nice": 5,
}, open(path, "wb"))
PY
}

case "$verb" in
  install)
    if ! is_macos; then
      say "linux: add this cron line (FFS never edits crontab):"; cron_line; exit 0
    fi
    write_plist
    "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || "$LAUNCHCTL" load -w "$PLIST" 2>/dev/null || fail "launchctl bootstrap failed for $PLIST"
    say "installed label=$LABEL repo=$REPO interval=${INTERVAL}s plist=$PLIST log=$LOG"
    ;;
  uninstall)
    is_macos || { say "linux: remove the reconcile cron line for $REPO yourself"; exit 0; }
    "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || "$LAUNCHCTL" unload "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    say "uninstalled label=$LABEL"
    ;;
  status)
    if ! is_macos; then
      if crontab -l 2>/dev/null | grep -Fq "cd $REPO && bash scripts/gsd/reconcile.sh"; then say "installed kind=cron repo=$REPO"; exit 0; fi
      say "not-installed kind=cron repo=$REPO"; exit 1
    fi
    if [ -f "$PLIST" ] && "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      say "installed label=$LABEL repo=$REPO interval=$(python3 -c 'import plistlib,sys; print(plistlib.load(open(sys.argv[1],"rb")).get("StartInterval"))' "$PLIST")s log=$LOG"
      exit 0
    fi
    if [ -f "$PLIST" ]; then say "plist-present-not-loaded label=$LABEL plist=$PLIST"; exit 1; fi
    say "not-installed label=$LABEL repo=$REPO"; exit 1
    ;;
  -h|--help|"") usage; [ -n "$verb" ] ;;
  *) usage >&2; fail "usage unknown-verb $verb" ;;
esac
