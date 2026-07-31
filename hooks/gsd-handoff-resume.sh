#!/usr/bin/env bash
# gsd-handoff-resume.sh — SessionStart HANDOFF surfacer (borrowed: buildomator).
#
# Companion to gsd-checkpoint.sh: when a session starts and a HANDOFF.json
# exists, print its content plus a resume pointer so the model re-anchors to
# the dead session's position instead of rediscovering it. The handoff is NOT
# deleted here — deleting on print would lose it if the pointer is ignored;
# the resume flow (or run-finalizer.sh clearing run-state) retires it.
#
# Contract: ALWAYS exit 0; silent when there is nothing to surface.
# Inert until a consumer wires it (SessionStart) in settings.json.
set -uo pipefail

HANDOFF=".planning/run-state/HANDOFF.json"
[ -f "$HANDOFF" ] || exit 0

echo "[gsd-handoff-resume] HANDOFF from a previous session found:"
cat "$HANDOFF" 2>/dev/null || true
echo "[gsd-handoff-resume] resume with /gsd-resume-work (Claude) or \$gsd-resume-work (Codex); stalled worktree -> /adopt-wip"
exit 0
