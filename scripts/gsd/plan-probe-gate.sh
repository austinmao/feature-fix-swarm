#!/usr/bin/env bash
# Fail before GSD execution when a phase's PLAN verify commands fail gsd-core's
# own deterministic plan probes.
#
# gsd-plan-phase runs these probes before its checker (plan-phase.md:
# `check verify-command-paths` and `check verify-failure-directions`), but a
# phase planned or revised outside that workflow — hand-orchestrated checker
# loops, hand-amended plans — reaches execution without them. Plans then ship
# <automated> commands with no <fails_when> statement, and the executor
# improvises what "failed" means (gsd-core #3172). This gate re-runs both
# probes at the execution boundary so every entry path is held to the same bar.
#
# Usage: plan-probe-gate.sh <phase-number>
# Honors GSD_PROJECT (gsd-core resolves .planning/<project>/ itself).
# Exit 0: both probes report zero blockers.
# Exit 1: a probe reports blockers, an unresolvable phase, or an unknown status.
# Exit 2: usage error.
# Exit 78: no gsd-tools that supports both probes was found (fail closed).
# No network is touched and no command text from the plans is executed.
set -uo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: plan-probe-gate.sh <phase-number>" >&2
  exit 2
fi

case "$1" in
  ''|*[!0-9]*)
    echo "plan-probe-gate: ERROR: phase must be numeric: $1" >&2
    exit 2
    ;;
esac

PHASE="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if ! command -v node >/dev/null 2>&1; then
  echo "plan-probe-gate: node not found on PATH — cannot run the plan probes, failing closed" >&2
  exit 78
fi

# Resolution order: the consumer's pinned package, the FFS runner layout, then
# the host installs. A candidate is used only when it answers both probes with
# a JSON payload, so an older gsd-core without them falls through to the next.
CANDIDATES=(
  "$REPO_ROOT/node_modules/.bin/gsd-tools"
  "$SCRIPT_DIR/../../node_modules/@opengsd/gsd-core/gsd-core/bin/gsd-tools.cjs"
  "$SCRIPT_DIR/../../packages/feature-fix-swarm/node_modules/@opengsd/gsd-core/gsd-core/bin/gsd-tools.cjs"
  "${HOME:-}/.claude/gsd-core/bin/gsd-tools.cjs"
  "${HOME:-}/.codex/gsd-core/bin/gsd-tools.cjs"
)

run_probe() { # $1 = gsd-tools path, $2 = probe name; prints the raw payload
  (cd "$REPO_ROOT" && node "$1" check "$2" "$PHASE" --raw 2>/dev/null)
}

is_payload() { # stdin = probe output; succeeds only for a JSON object with "status"
  python3 -c '
import json, sys
text = sys.stdin.read()
start = text.find("{")
try:
    data = json.loads(text[start:]) if start >= 0 else None
except ValueError:
    data = None
sys.exit(0 if isinstance(data, dict) and "status" in data else 1)
'
}

GSD_TOOLS=""
PATHS_JSON=""
DIRECTIONS_JSON=""
TRIED=()
for candidate in "${CANDIDATES[@]}"; do
  [ -e "$candidate" ] || continue
  TRIED+=("$candidate")
  paths_out="$(run_probe "$candidate" verify-command-paths)"
  printf '%s' "$paths_out" | is_payload || continue
  directions_out="$(run_probe "$candidate" verify-failure-directions)"
  printf '%s' "$directions_out" | is_payload || continue
  GSD_TOOLS="$candidate"
  PATHS_JSON="$paths_out"
  DIRECTIONS_JSON="$directions_out"
  break
done

if [ -z "$GSD_TOOLS" ]; then
  echo "plan-probe-gate: no gsd-tools answered 'check verify-command-paths' and 'check verify-failure-directions' (gsd-core 1.15+ required) — failing closed" >&2
  if [ "${#TRIED[@]}" -eq 0 ]; then
    echo "plan-probe-gate: no gsd-tools found; looked in: ${CANDIDATES[*]}" >&2
  else
    echo "plan-probe-gate: tried: ${TRIED[*]}" >&2
  fi
  exit 78
fi

PATHS_FILE="$(mktemp)"
DIRECTIONS_FILE="$(mktemp)"
trap 'rm -f "$PATHS_FILE" "$DIRECTIONS_FILE"' EXIT
printf '%s' "$PATHS_JSON" > "$PATHS_FILE"
printf '%s' "$DIRECTIONS_JSON" > "$DIRECTIONS_FILE"

python3 - "$PHASE" "${GSD_PROJECT:-}" "$PATHS_FILE" "$DIRECTIONS_FILE" <<'PY'
from __future__ import annotations

import json
import sys

phase, project, paths_file, directions_file = sys.argv[1:5]
PASS_STATUSES = {"ok", "not_applicable"}


def load(path: str) -> dict:
    text = open(path, encoding="utf-8").read()
    return json.loads(text[text.find("{"):])


def evaluate(name: str, data: dict) -> tuple[list[str], int]:
    status = data.get("status")
    counts = data.get("counts") or {}
    blockers = int(counts.get("blocker") or 0)
    problems: list[str] = []
    if status == "unresolvable":
        reason = data.get("readError") or "phase directory not found"
        problems.append(f"{name}: phase {phase} is unresolvable ({reason})")
    elif status not in PASS_STATUSES and status != "blocked":
        problems.append(f"{name}: unknown probe status {status!r} — failing closed")
    for command in data.get("commands") or []:
        if command.get("severity") != "blocker":
            continue
        text = " ".join(str(command.get("command") or "").split())
        if len(text) > 100:
            text = text[:97] + "..."
        where = command.get("plan") or "?"
        task = command.get("task")
        if task:
            where += f" / {str(task)[:60]}"
        detail = command.get("reason") or command.get("status") or "blocker"
        problems.append(f"{name}: {where}: {detail}: {text}")
    if status == "blocked" and not problems:
        problems.append(f"{name}: status blocked ({blockers} blockers)")
    return problems, int(counts.get("total") or 0)


paths_problems, paths_total = evaluate("verify-command-paths", load(paths_file))
directions_problems, directions_total = evaluate("verify-failure-directions", load(directions_file))
problems = paths_problems + directions_problems
scope = f" project={project}" if project else ""

if problems:
    for problem in problems:
        print(f"plan-probe-gate: BLOCKER: {problem}", file=sys.stderr)
    print(
        f"plan-probe-gate: FAIL phase={int(phase):02d}{scope} blockers={len(problems)} — "
        "give each runnable <automated> its own <fails_when> naming the observable failure "
        "signal (gsd-core references/planner-failing-direction.md) and make every command "
        "target resolve, then re-run.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    f"plan-probe-gate: PASS phase={int(phase):02d}{scope} "
    f"command_paths={paths_total} failure_directions={directions_total}"
)
PY
