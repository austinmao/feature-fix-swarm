#!/usr/bin/env bash
# cli-hang-guard.sh — PreToolUse (Bash) blocker for unbounded vendor-CLI calls.
#
# WHY: the 2026-07-12 spec-298 stall was the ORCHESTRATOR running `codex` ad-hoc
# from its Bash tool — outside adversary-host.sh, so none of the lever's
# timeout/stdin guards applied, and the session blocked >30 minutes on a dead
# process. Guidance alone did not hold; this hook is the enforcement point
# (sibling of delegation-enforcer.sh — same fail-open + kill-switch pattern).
#
# Blocks (exit 2) Bash commands invoking the hang-prone EXECUTION forms
#     codex exec | codex review | claude -p / --print
# without a visible wall-clock bound (timeout/gtimeout/run_bounded) and outside
# the sanctioned levers (adversary-host.sh, gsd-run.sh, plan-adversary.sh,
# qa-coverage-adversary.sh, review-gate-command.sh, model-fallback.sh — all
# internally bounded via run-bounded.sh). Non-execution probes pass untouched:
# `codex --version`, `codex login status`, `claude --version`.
#
# Fail-open: empty/unparseable input or missing python3 -> exit 0 (never
# blocks unrelated Bash). Kill-switch: CLI_HANG_GUARD=off.
set -uo pipefail

[ "${CLI_HANG_GUARD:-}" = "off" ] && exit 0

INPUT=$(cat 2>/dev/null || true)
[ -z "$INPUT" ] && exit 0

CMD=$(printf '%s' "$INPUT" | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("command", ""))
except Exception:
    pass' 2>/dev/null) || exit 0
[ -z "$CMD" ] && exit 0

# Already-bounded forms and sanctioned levers pass.
# (`*timeout *` also covers `gtimeout ` as a substring — SC2221)
case "$CMD" in
  *timeout\ *|*run_bounded*|*adversary-host*|*gsd-run.sh*|*plan-adversary*|*qa-coverage-adversary*|*review-gate-command*|*model-fallback*) exit 0 ;;
esac

# Execution forms only — version/status probes don't match these patterns.
if printf '%s' "$CMD" | grep -qE '(^|[[:space:];&|(])codex[[:space:]]+(exec|review)([[:space:]]|$)' || \
   printf '%s' "$CMD" | grep -qE '(^|[[:space:];&|(])claude[[:space:]][^;&|]*(-p|--print)([[:space:]]|$|")'; then
  cat >&2 <<'MSG'
[cli-hang-guard] BLOCKED: bare codex/claude execution without a wall-clock bound.
A hung CLI subprocess blocks the session indefinitely (2026-07-12 dead-codex stall
— a 30-minute "review" that was a dead-process wait).
Fix one of:
  - wrap it:        timeout 540 codex exec ... </dev/null    (bare exit code — never pipe the live call)
  - use the lever:  scripts/gsd/adversary-host.sh (adversary_invoke) / scripts/gsd/gsd-run.sh
Kill-switch (operator only): CLI_HANG_GUARD=off
MSG
  exit 2
fi
exit 0
