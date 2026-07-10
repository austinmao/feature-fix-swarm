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
# Usage: registered as a PreToolUse hook for Agent|Task; reads the tool_use
# JSON envelope on stdin, writes the (possibly modified) envelope to stdout.
#
# Kill-switch: DELEGATION_ENFORCER=off passes stdin through unconditionally.
set -euo pipefail

if [ "${DELEGATION_ENFORCER:-}" = "off" ]; then
  cat
  exit 0
fi

INPUT=$(cat)

python3 - "$INPUT" <<'PY'
import json, sys
from pathlib import Path

raw = sys.argv[1]

try:
    data = json.loads(raw)
except Exception:
    print(raw, end="")
    sys.exit(0)

if data.get("tool_name") not in ("Agent", "Task"):
    print(raw, end="")
    sys.exit(0)

ti = data.get("tool_input", {}) or {}

if ti.get("model"):
    print(raw, end="")
    sys.exit(0)

subagent_type = ti.get("subagent_type")

try:
    config = json.loads(Path(".planning/config.json").read_text())
except Exception:
    print("[delegation-enforcer] WARN: no readable .planning/config.json — passthrough unpinned",
          file=sys.stderr)
    print(raw, end="")
    sys.exit(0)

overrides = config.get("model_overrides")
if not overrides:
    print("[delegation-enforcer] WARN: config.json has no model_overrides — passthrough unpinned",
          file=sys.stderr)
    print(raw, end="")
    sys.exit(0)

model = overrides.get(subagent_type)
if not model:
    print(f"[delegation-enforcer] WARN: no model_overrides entry for subagent_type={subagent_type!r} — passthrough unpinned",
          file=sys.stderr)
    print(raw, end="")
    sys.exit(0)

ti["model"] = model
data["tool_input"] = ti
print(json.dumps(data), end="")
sys.exit(0)
PY
