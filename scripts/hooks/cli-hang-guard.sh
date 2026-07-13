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

# Parse tokens as data; never eval the command. A bound only protects the
# simple command it actually wraps. Substrings in an echo, path, or env value
# cannot bless a later bare CLI invocation. Recurse into shell -c payloads.
python3 - "$CMD" <<'PY'
import os
import re
import shlex
import sys

ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SEPARATORS = {";", "&", "&&", "|", "||", "(", ")"}


def tokens(command):
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True,
                        punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def segments(command):
    current = []
    for token in tokens(command):
        if token in SEPARATORS or (token and set(token) <= set(";&|()")):
            if current:
                yield current
                current = []
        else:
            current.append(token)
    if current:
        yield current


def command_index(segment):
    i = 0
    while i < len(segment) and ASSIGN.match(segment[i]):
        i += 1
    if i < len(segment) and os.path.basename(segment[i]) == "env":
        i += 1
        while i < len(segment):
            token = segment[i]
            if token in ("-u", "--unset"):
                i += 2
            elif token.startswith("-") or ASSIGN.match(token):
                i += 1
            else:
                break
    if i < len(segment) and os.path.basename(segment[i]) in ("command", "exec"):
        i += 1
        while i < len(segment) and segment[i].startswith("-"):
            i += 1
    return i


def vendor_executable(token):
    base = os.path.basename(token)
    if base == "codex":
        return "codex"
    if base == "claude":
        return "claude"
    # Commands often route through configured executable variables. Treat a
    # variable whose name identifies the vendor as that executable; braces do
    # not change the decision. Do not expand or execute the value.
    match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", token)
    if match:
        name = match.group(1).upper()
        if "CODEX" in name:
            return "codex"
        if "CLAUDE" in name:
            return "claude"
    return ""


def unsafe(command, inherited_bound=False, depth=0):
    if depth > 3:
        return True
    try:
        parsed = list(segments(command))
    except ValueError:
        return False  # malformed/incomplete shell text stays fail-open
    for segment in parsed:
        i = command_index(segment)
        if i >= len(segment):
            continue
        executable = os.path.basename(segment[i])
        bounded = inherited_bound or executable in ("timeout", "gtimeout", "run_bounded")
        if bounded:
            continue

        # Detect direct and wrapper-prefixed execution forms conservatively.
        for pos, token in enumerate(segment):
            vendor = vendor_executable(token)
            if vendor == "codex" and pos + 1 < len(segment) and segment[pos + 1] in ("exec", "review"):
                return True
            if vendor == "claude" and any(arg in ("-p", "--print") for arg in segment[pos + 1:]):
                return True

        # Shell -c hides its payload in one quoted token; inspect it recursively.
        if executable in ("bash", "sh", "zsh"):
            try:
                cpos = segment.index("-c", i + 1)
            except ValueError:
                continue
            if cpos + 1 < len(segment) and unsafe(segment[cpos + 1], False, depth + 1):
                return True
    return False


try:
    blocked = unsafe(sys.argv[1])
except Exception:
    blocked = False
sys.exit(2 if blocked else 0)
PY
_guard_rc=$?

if [ "$_guard_rc" -eq 2 ]; then
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
