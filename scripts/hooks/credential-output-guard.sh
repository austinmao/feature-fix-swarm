#!/usr/bin/env bash
# credential-output-guard.sh — PreToolUse blocker for commands that print
# credential values into Claude/Codex transcripts.
#
# Blocks Doppler secret-value reads/downloads and Railway variable/config JSON
# dumps. `doppler run` remains the preferred non-printing injection path;
# `doppler secrets --only-names` remains available for inventory. The command
# is parsed as data and never evaluated. Empty/malformed envelopes fail open.
set -uo pipefail

[ "${CREDENTIAL_OUTPUT_GUARD:-}" = "off" ] && exit 0

# The hook protocol is one JSON envelope per line. Do not read to EOF: Codex
# may retain the write end until this process exits, which otherwise deadlocks
# until the host's hook timeout. One bounded read preserves fail-open behavior.
INPUT=""
IFS= read -r -t 3 INPUT 2>/dev/null || true
[ -z "$INPUT" ] && exit 0

CMD=$(printf '%s' "$INPUT" | python3 -c 'import json, sys
try:
    payload = json.load(sys.stdin).get("tool_input", {})
    print(payload.get("command") or payload.get("cmd") or "")
except Exception:
    pass' 2>/dev/null) || exit 0
[ -z "$CMD" ] && exit 0

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


def command_substitutions(command):
    found = []
    i = 0
    quote = None
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "'" and quote is None:
            quote = "'"
            i += 1
            continue
        if ch == '"':
            quote = None if quote == '"' else '"'
            i += 1
            continue
        if ch == "`":
            j = i + 1
            while j < len(command):
                if command[j] == "\\":
                    j += 2
                    continue
                if command[j] == "`":
                    found.append(command[i + 1:j])
                    i = j + 1
                    break
                j += 1
            else:
                found.append(command[i + 1:])
                return found
            continue
        if ch == "$" and i + 1 < len(command) and command[i + 1] == "(":
            start = i + 2
            j = start
            nested = 1
            inner_quote = None
            while j < len(command):
                current = command[j]
                if inner_quote == "'":
                    if current == "'":
                        inner_quote = None
                    j += 1
                    continue
                if current == "\\":
                    j += 2
                    continue
                if current == "'" and inner_quote is None:
                    inner_quote = "'"
                    j += 1
                    continue
                if current == '"':
                    inner_quote = None if inner_quote == '"' else '"'
                    j += 1
                    continue
                if current == "(":
                    nested += 1
                elif current == ")":
                    nested -= 1
                    if nested == 0:
                        found.append(command[start:j])
                        i = j + 1
                        break
                j += 1
            else:
                found.append(command[start:])
                return found
            continue
        i += 1
    return found


def command_index(segment):
    i = 0
    while i < len(segment) and ASSIGN.match(segment[i]):
        i += 1
    if i < len(segment) and os.path.basename(segment[i]) in ("command", "exec"):
        i += 1
        while i < len(segment) and segment[i].startswith("-"):
            i += 1
    return i


def unwrap_rtk(segment, i):
    if i >= len(segment) or os.path.basename(segment[i]) != "rtk":
        return segment, i
    i += 1
    if i < len(segment) and segment[i] in ("proxy", "summary", "err", "test"):
        i += 1
    return segment, i


def unwrap_wrappers(segment, i):
    while i < len(segment):
        executable = os.path.basename(segment[i])
        if executable in ("env",):
            i += 1
            while i < len(segment):
                token = segment[i]
                if token == "--":
                    i += 1
                    break
                if token in ("-u", "--unset", "-C", "--chdir"):
                    i += 2
                    continue
                if token in ("-S", "--split-string") and i + 1 < len(segment):
                    replacement = shlex.split(segment[i + 1])
                    segment = segment[:i] + replacement + segment[i + 2:]
                    break
                if token.startswith("--split-string="):
                    replacement = shlex.split(token.split("=", 1)[1])
                    segment = segment[:i] + replacement + segment[i + 1:]
                    break
                if token.startswith("-") or ASSIGN.match(token):
                    i += 1
                    continue
                break
            continue
        if executable in ("command", "exec"):
            i += 1
            while i < len(segment) and segment[i].startswith("-"):
                i += 1
            continue
        if executable == "rtk":
            segment, i = unwrap_rtk(segment, i)
            continue
        if executable in ("timeout", "gtimeout"):
            i += 1
            while i < len(segment):
                token = segment[i]
                if token == "--":
                    i += 1
                    break
                if token in ("-k", "--kill-after", "-s", "--signal"):
                    i += 2
                    continue
                if token.startswith("--kill-after=") or token.startswith("--signal="):
                    i += 1
                    continue
                if token.startswith("-"):
                    i += 1
                    continue
                i += 1  # duration
                break
            continue
        if executable == "time":
            i += 1
            while i < len(segment):
                token = segment[i]
                if token == "--":
                    i += 1
                    break
                if token in ("-f", "--format", "-o", "--output"):
                    i += 2
                    continue
                if token.startswith("-"):
                    i += 1
                    continue
                break
            continue
        if executable == "stdbuf":
            i += 1
            while i < len(segment) and segment[i].startswith("-"):
                token = segment[i]
                if token in ("-i", "-o", "-e"):
                    i += 2
                else:
                    i += 1
            continue
        if executable == "setsid":
            i += 1
            while i < len(segment) and segment[i].startswith("-"):
                i += 1
            continue
        if executable == "sudo":
            i += 1
            value_options = {
                "-u", "--user", "-g", "--group", "-h", "--host",
                "-p", "--prompt", "-C", "--close-from", "-R", "--chroot",
                "-D", "--chdir", "-T", "--command-timeout",
            }
            while i < len(segment):
                token = segment[i]
                if token == "--":
                    i += 1
                    break
                if token in value_options:
                    i += 2
                    continue
                if token.startswith("-"):
                    i += 1
                    continue
                if ASSIGN.match(token):
                    i += 1
                    continue
                break
            continue
        if executable == "nice":
            i += 1
            while i < len(segment):
                token = segment[i]
                if token == "--":
                    i += 1
                    break
                if token in ("-n", "--adjustment"):
                    i += 2
                    continue
                if token.startswith("--adjustment=") or re.match(r"^-[0-9]+$", token):
                    i += 1
                    continue
                break
            continue
        if executable == "nohup":
            i += 1
            if i < len(segment) and segment[i] == "--":
                i += 1
            continue
        break
    return segment, i


def shell_payload(args):
    i = 0
    value_options = {"-o", "+o", "-O", "--rcfile", "--init-file"}
    while i < len(args):
        token = args[i]
        if token == "--":
            return None
        if token in value_options:
            i += 2
            continue
        if re.match(r"^[+-][A-Za-z]+$", token) and "c" in token.lstrip("+-"):
            payload_index = i + 1
            if payload_index < len(args) and args[payload_index] == "--":
                payload_index += 1
            return args[payload_index] if payload_index < len(args) else ""
        i += 1
    return None


def unsafe_segment(segment, depth=0):
    if depth > 4:
        return True
    i = command_index(segment)
    segment, i = unwrap_wrappers(segment, i)
    if i >= len(segment):
        return False
    executable = os.path.basename(segment[i])
    args = segment[i + 1:]

    if executable in ("bash", "sh", "zsh"):
        payload = shell_payload(args)
        return payload is not None and (payload == "" or unsafe(payload, depth + 1))

    if executable == "eval":
        payload = " ".join(args)
        if not payload:
            return False
        if any(marker in payload for marker in ("$", "`", "\\n", "\\r")):
            return True
        return unsafe(payload, depth + 1)

    if executable == "doppler":
        if "secrets" in args:
            secret_pos = args.index("secrets")
            secret_args = args[secret_pos + 1:]
            if "--only-names" not in secret_args:
                return True
            if any(arg in ("get", "download") for arg in secret_args):
                return True
        if "run" in args and "--" in args:
            nested = args[args.index("--") + 1:]
            return bool(nested) and unsafe_segment(nested, depth + 1)
        return False

    if executable == "railway":
        if any(arg in ("variable", "variables") for arg in args):
            return True
        if "config" in args and any(arg == "--json" or arg.startswith("--json=") for arg in args):
            return True
    return False


def unsafe(command, depth=0):
    try:
        if any(unsafe(payload, depth + 1)
               for payload in command_substitutions(command)):
            return True
        return any(unsafe_segment(segment, depth) for segment in segments(command))
    except (ValueError, IndexError):
        return False


sys.exit(2 if unsafe(sys.argv[1]) else 0)
PY
guard_rc=$?

if [ "$guard_rc" -eq 2 ]; then
  cat >&2 <<'MSG'
[credential-output-guard] BLOCKED: this command can print credential values into the agent transcript.
Use one of:
  - inject without printing:  doppler run -p <project> -c <config> -- <command>
  - list names only:          doppler secrets --only-names
  - query a single non-secret Railway field through a sanitized lever
Kill-switch (operator only): CREDENTIAL_OUTPUT_GUARD=off
MSG
  exit 2
fi
exit 0
