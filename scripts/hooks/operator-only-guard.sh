#!/usr/bin/env bash
# operator-only-guard.sh — PreToolUse (Bash/shell) deny for gate commands that
# only the operator may run (FFS #253): findings-queue resolve --disposition
# issue|descope, findings-queue retag --in-spec, intent-check --repin.
#
# ANTI-ACCIDENT ONLY, not a security boundary: indirection via script files or
# python imports bypasses it. Tokenizes with shlex (no eval); on a quoting error it falls back to raw text. Known false
# positive: a segment that merely contains the words (echo, heredoc text) is
# denied. No kill-switch. Malformed or empty input fails open.
set -uo pipefail

# One bounded read: hosts may keep the pipe open, so never read to EOF.
INPUT=""
IFS= read -r -t 3 INPUT 2>/dev/null || true
[ -z "$INPUT" ] && exit 0
command -v python3 >/dev/null 2>&1 || exit 0

WHICH=$(printf '%s' "$INPUT" | python3 -c 'import json, re, shlex, sys
def segments(cmd):
    cmd = cmd.replace("\\\n", " ").replace("`", " ; ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split, lex.commenters = True, ""
        seg = []
        for tok in lex:
            if tok.strip(";&|()<>"):
                seg.append(tok)
            else:
                yield " ".join(seg).split()
                seg = []
        yield " ".join(seg).split()
    except ValueError:
        for raw in re.split(r"[;&|()]", cmd.replace("\"", "").replace("\x27", "")):
            yield raw.split()
try:
    ti = json.load(sys.stdin).get("tool_input", {})
    cmd = ti.get("command") or ti.get("cmd") or ""
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    for t in segments(str(cmd)):
        if "findings-queue" in t and "resolve" in t and any(
                a == "--disposition" and b in ("issue", "descope")
                for a, b in zip(t, t[1:])):
            print("findings-queue resolve --disposition issue|descope")
        elif "findings-queue" in t and "retag" in t and "--in-spec" in t:
            print("findings-queue retag --in-spec")
        elif "intent-check" in t and "--repin" in t:
            print("intent-check --repin")
        else:
            continue
        break
except Exception:
    pass' 2>/dev/null) || exit 0

[ -z "$WHICH" ] && exit 0
echo "operator-only-guard: $WHICH is operator-only; the operator runs it in their own shell (FFS #253)" >&2
exit 2
