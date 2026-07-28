#!/usr/bin/env bash
# spec-status deterministic collector — READ-ONLY. Emits labeled fact sections
# for the spec-status skill's subagents to interpret. Never mutates anything.
# Usage: collect-status-facts.sh <spec-id-or-slug-prefix>   (run from repo/worktree root)
set -u
SPEC="${1:-}"
[ -n "$SPEC" ] || { echo "usage: collect-status-facts.sh <spec-id>"; exit 1; }
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT" || exit 1
SPEC_DIR="$(find specs -maxdepth 1 -type d -name "${SPEC%%-*}-*" 2>/dev/null | head -1)"

echo "== GIT =="
git branch --show-current
git log --oneline -8
git status --porcelain | head -30
echo "ahead-behind: $(git rev-list --left-right --count @{upstream}...HEAD 2>/dev/null || echo 'no upstream')"

echo "== PLANNING =="
if [ -d .planning ]; then
  grep -E '^\s*- \[.\]' .planning/ROADMAP.md 2>/dev/null | head -20
  for d in .planning/phases/*/; do
    p=$(ls "$d" 2>/dev/null | grep -c 'PLAN.md$'); s=$(ls "$d" 2>/dev/null | grep -c 'SUMMARY.md$')
    echo "phase $(basename "$d"): plans=$p summaries=$s"
  done
  head -12 .planning/STATE.md 2>/dev/null
else
  echo "no .planning/"
fi

echo "== RUNNER =="
if [ -f .planning/run-state/gsd-run.status ]; then
  cat .planning/run-state/gsd-run.status
  PID=$(head -1 .planning/run-state/gsd-run.pid 2>/dev/null)
  kill -0 "$PID" 2>/dev/null && echo "pid-liveness: ALIVE ($PID)" || echo "pid-liveness: DEAD ($PID)"
else
  echo "no runner state"
fi

echo "== LEDGER =="
# Caller must export GATES_STORE (MAIN store) + GSD_RUN_ID before invoking.
echo "GATES_STORE=${GATES_STORE:-UNSET-DECOY-RISK}"
GP=""
for c in "$ROOT/packages/feature-fix-swarm/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py" "$ROOT/lib/gates.py"; do
  [ -f "$c" ] && GP="$c" && break
done
if [ -n "$GP" ] && [ -n "${GSD_RUN_ID:-}" ]; then
  python3 "$GP" pending "$GSD_RUN_ID" 2>&1 | head -25
else
  echo "gates.py or GSD_RUN_ID unavailable"
fi

echo "== EVIDENCE =="
[ -n "$SPEC_DIR" ] && ls -lt "$SPEC_DIR/evidence/" 2>/dev/null | head -15 || echo "no spec dir for $SPEC"

echo "== HYGIENE =="
# Key-shaped strings in evidence/ (pcp_ prefix caught live 2026-07-28; sk-/Bearer classic shapes).
# Filenames only — never print matched content.
[ -n "$SPEC_DIR" ] && grep -rlE '(pcp_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9._-]{20,})' "$SPEC_DIR/evidence/" 2>/dev/null | head -5 || true
echo "hygiene-scan-done"

echo "== DISK =="
df -h "${TMPDIR:-/tmp}" | tail -1
df -h "$ROOT" | tail -1
