#!/usr/bin/env bash
# operator-only-guard.sh — PreToolUse (Bash/shell) deny for gate commands that
# only the operator may run (FFS #253): findings-queue resolve --disposition
# issue|descope, findings-queue retag --in-spec, intent-check --repin, and the
# cloud-delegation gates: claude --cloud, codex cloud exec|apply|diff,
# cloud-dispatch adopt|abandon|resolve (token membership, like the rest).
#
# ANTI-ACCIDENT ONLY, not a security boundary: indirection via script files or
# python imports bypasses it. Tokenizes with shlex (no eval); on a quoting error it falls back to raw text. Known false
# positive: for the three original gates, a segment that merely contains the
# words (echo, heredoc text) is denied. The three cloud gates match by EXECUTABLE
# POSITION instead: the first token of the segment after leading VAR=value
# assignments and wrappers (env, command, exec, sudo, nohup, time, rtk [proxy]) and
# after shell/python launchers (bash/sh -c BODY, bash SCRIPT, python3 SCRIPT),
# compared by basename, so `git grep -e codex -e cloud -e diff` is allowed and
# `/opt/homebrew/bin/claude --cloud` is denied. No kill-switch. Malformed or
# empty input fails open.
set -uo pipefail

# One bounded read: hosts may keep the pipe open, so never read to EOF.
INPUT=""
IFS= read -r -t 3 INPUT 2>/dev/null || true
[ -z "$INPUT" ] && exit 0
command -v python3 >/dev/null 2>&1 || exit 0

WHICH=$(printf '%s' "$INPUT" | python3 -c 'import json, os, re, shlex, sys
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
WRAPPERS = ("env", "command", "exec", "sudo", "nohup", "time", "rtk", "nice", "stdbuf")
SHELLS = ("bash", "sh", "zsh", "dash", "ksh")
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
def exe_tail(t):
    """Tokens from the executable position: the command that really runs."""
    i, n = 0, len(t)
    while i < n:
        tok, base = t[i], os.path.basename(t[i])
        if ASSIGN.match(tok):
            i += 1
        elif base in WRAPPERS:
            i += 1
            while i < n and (t[i].startswith("-") or ASSIGN.match(t[i]) or (base == "rtk" and t[i] == "proxy")):
                i += 1
        elif base in SHELLS or re.match(r"^python[0-9.]*$", base):
            j = i + 1
            while j < n and t[j].startswith("-"):
                if base in SHELLS and re.match(r"^-[a-z]*c$", t[j]):
                    j += 1
                    break
                j += 1
            if j >= n:
                break
            i = j
        else:
            break
    return t[i:]
CLOUD_VERBS = ("exec", "apply", "diff")
DISPATCH_VERBS = ("adopt", "abandon", "resolve")
def one_gate(t):
    e = exe_tail(t)
    if not e:
        return ""
    exe, rest = os.path.basename(e[0]), e[1:]
    if exe == "claude" and any(a == "--cloud" or a.startswith("--cloud=") for a in rest):
        return "claude --cloud"
    if exe == "codex":
        sub = [a for a in rest if not a.startswith("-")]
        if len(sub) >= 2 and sub[0] == "cloud" and sub[1] in CLOUD_VERBS:
            return "codex cloud " + sub[1]
    if exe in ("cloud-dispatch.sh", "cloud_dispatch.py"):
        for v in DISPATCH_VERBS:
            if v in rest:
                return "cloud-dispatch " + v
    return ""
def cloud_gate(t):
    """Label of the cloud gate this segment runs, else empty. Executable position only.

    A bash -c BODY is flattened into the segment by the tokenizer, so the body
    may hold several commands: each part between control operators is checked.
    """
    part = []
    for tok in list(t) + [";"]:
        if tok in (";", "&&", "||", "|", "&"):
            label = one_gate(part) if part else ""
            if label:
                return label
            part = []
        else:
            part.append(tok)
    return ""
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
        elif cloud_gate(t):
            print(cloud_gate(t))
        else:
            continue
        break
except Exception:
    pass' 2>/dev/null) || exit 0

[ -z "$WHICH" ] && exit 0
echo "operator-only-guard: $WHICH is operator-only; the operator runs it in their own shell (FFS #253)" >&2
exit 2
