#!/usr/bin/env bash
# delegation-enforcer.sh — PreToolUse (Agent|Task) advisory model auto-pin.
#
# WHY: unpinned sub-agent spawns drift to whatever the harness's default
# model is (often a premium tier), silently burning cost across a session.
# This hook is the spawn-time prevention point: pin the model from
# .planning/config.json model_overrides[subagent_type] BEFORE the spawn
# happens, instead of catching it after the fact in a delegation-audit
# report. Advisory only — never blocks a spawn, never crashes (fail-open,
# mirrors gsd-phase-evidence-gate.sh's stdin/python3-heredoc pattern).
#
# PreToolUse hook contract (Claude Code): a mutation is signalled by a hook
# RESPONSE on stdout —
#   {"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":{...}}}
# where updatedInput is the replacement tool_input. Emitting NOTHING on stdout
# means "no change" (passthrough). So: inject -> emit that envelope; every
# passthrough path -> emit nothing, exit 0.
#
# Usage: registered as a PreToolUse hook for Agent|Task; reads the tool_use
# JSON envelope on stdin.
#
# Kill-switch: DELEGATION_ENFORCER=off -> unconditional passthrough (no output).
set -euo pipefail

if [ "${DELEGATION_ENFORCER:-}" = "off" ]; then
  exit 0
fi

INPUT=$(cat)

# `set -e` + a here-doc python3 would BLOCK the spawn if python3 is missing or
# killed (nonzero exit propagates). Guard it: any python3 failure -> WARN +
# passthrough (empty stdout), never a block. The RHS runs only when python3
# itself fails to run; a clean passthrough/mutation exits 0 above.
python3 - "$INPUT" <<'PY' || { echo "[delegation-enforcer] WARN: python3 failed — passthrough unpinned" >&2; exit 0; }
import json, sys
from pathlib import Path

raw = sys.argv[1]

def passthrough():
    # empty stdout = "no change" per the PreToolUse hook contract.
    sys.exit(0)

try:
    data = json.loads(raw)
except Exception:
    passthrough()

if data.get("tool_name") not in ("Agent", "Task"):
    passthrough()

ti = data.get("tool_input", {}) or {}

if ti.get("model"):
    passthrough()

subagent_type = ti.get("subagent_type")

try:
    config = json.loads(Path(".planning/config.json").read_text())
except Exception:
    print("[delegation-enforcer] WARN: no readable .planning/config.json — passthrough unpinned",
          file=sys.stderr)
    passthrough()

overrides = config.get("model_overrides")
if not overrides:
    print("[delegation-enforcer] WARN: config.json has no model_overrides — passthrough unpinned",
          file=sys.stderr)
    passthrough()

model = overrides.get(subagent_type)
if not model:
    print(f"[delegation-enforcer] WARN: no model_overrides entry for subagent_type={subagent_type!r} — passthrough unpinned",
          file=sys.stderr)
    passthrough()

ti["model"] = model
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": ti,
    }
}), end="")
sys.exit(0)
PY
