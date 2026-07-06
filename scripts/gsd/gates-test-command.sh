#!/usr/bin/env bash
# gsd workflow.test_command target — invoked by gsd-core's execute-phase/verify-phase
# gates (references/planning-config.md:267). exit 0 = pass, non-zero = block.
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PHASE_ID="${1:-${GSD_PHASE_ID:-gsd-phase}}"

GATES_PY=""
for candidate in \
  "$REPO_ROOT/packages/feature-fix-swarm/lib/gates.py" \
  "$HOME/.claude/lib/feature-fix-swarm/gates.py" \
  "$REPO_ROOT/lib/gates.py"; do
  if [ -f "$candidate" ]; then
    GATES_PY="$candidate"
    break
  fi
done

if [ -z "$GATES_PY" ]; then
  echo "gates-test-command: gates.py not found in any known FFS install location" >&2
  exit 1
fi

# Test command resolution: GSD_TEST_CMD env > .planning/gsd-test-command file
# > repo lib/tests. NO vacuous fallback — a syntax-check-of-self used to record
# verify-done evidence that unlocked phase flips without testing anything
# (evidence laundering; caught in the 2026-07-06 fable review).
if [ -n "${GSD_TEST_CMD:-}" ]; then
  TEST_CMD=(bash -c "$GSD_TEST_CMD")
elif [ -f "$REPO_ROOT/.planning/gsd-test-command" ]; then
  TEST_CMD=(bash -c "$(cat "$REPO_ROOT/.planning/gsd-test-command")")
elif [ -d "$REPO_ROOT/lib/tests" ]; then
  TEST_CMD=(python3 -m pytest lib/tests -q)
else
  echo "gates-test-command: no test command — set GSD_TEST_CMD or write one to .planning/gsd-test-command (refusing to record vacuous evidence)" >&2
  exit 1
fi

# No GATES_STRICT on run-gate: it exports into the child test env, and 4
# lib/tests/test_gates.py tests are GATES_STRICT-sensitive (fail under it).
# Strictness only matters at verify-done (rejects caller-recorded evidence).
python3 "$GATES_PY" run-gate "$PHASE_ID" -- "${TEST_CMD[@]}"
run_rc=$?
if [ $run_rc -ne 0 ]; then
  exit $run_rc
fi

GATES_STRICT=1 python3 "$GATES_PY" verify-done "$PHASE_ID"
exit $?
