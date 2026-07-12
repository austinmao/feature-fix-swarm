#!/usr/bin/env bash
# run-bounded.sh — sourceable lib: hard wall-clock bound for external CLI calls.
#
# WHY: a hung codex/claude subprocess blocks its caller forever. The spec-298
# "30-minute adversarial review" (2026-07-12 forensics) was a dead-process
# wait, not review time. Stock macOS/BSD ships neither `timeout` nor
# `gtimeout`, and the old call sites deliberately ran UNWRAPPED when both were
# absent — turning a missing binary into an unbounded foreground hang that the
# downstream fail-open rc handling never sees (the call never returns).
#
# Resolution ladder (first hit wins):
#   1. timeout   (GNU coreutils / Linux)
#   2. gtimeout  (brew coreutils on macOS)
#   3. python3   subprocess.run(timeout=) — kills the child, exits 124
#   4. REFUSE    rc 124 without running the command at all
# Never runs the command unbounded. rc 124 is the same code the callers'
# existing timeout/fail-open paths already handle, so a refused call degrades
# to "adversary skipped (logged)" instead of a silent forever-block.
#
# NOTE on the refuse default: a SKIPPED adversary on a coreutils-less host is
# a deliberate fail-open choice, matching every caller's existing degrade
# philosophy. If a mandatory-adversary policy (high-blast plans) should
# hard-stop instead, gate that at the CALLER on rc 124 — this lib stays a pure
# bounding primitive.
#
# Usage: . run-bounded.sh; run_bounded <seconds> <cmd> [args...]
# stdin/stdout/stderr pass through — keep `</dev/null` at call sites (stdin-wait
# is a separate hang class from wall-clock).

run_bounded() {
  local _t="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$_t" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$_t" "$@"
  elif command -v python3 >/dev/null 2>&1; then
    # -c (not a heredoc) so the child inherits the caller's stdin untouched.
    python3 -c 'import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=float(sys.argv[1])).returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
except FileNotFoundError:
    sys.exit(127)' "$_t" "$@"
  else
    echo "[run-bounded] no timeout mechanism (timeout/gtimeout/python3 all absent) — refusing unbounded '$1'; treating as timeout (rc 124)" >&2
    return 124
  fi
}
