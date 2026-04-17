#!/usr/bin/env bash
set -euo pipefail

# QA Swarm Orchestrator — spawns 3 LLM agents + 2 deterministic hooks per phase
# Usage: bash scripts/qa-swarm.sh --phase "Phase 2" --diff "file1.ts file2.py" --spec-dir specs/082-foo [--qa-skip e2e] [--qa-only review,security] [--max-retries 3]

PHASE=""
DIFF_FILES=""
SPEC_DIR=""
QA_SKIP=""
QA_ONLY=""
MAX_RETRIES=3
RALPH_DIR=".ralph"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2 ;;
    --diff) DIFF_FILES="$2"; shift 2 ;;
    --spec-dir) SPEC_DIR="$2"; shift 2 ;;
    --qa-skip) QA_SKIP="$2"; shift 2 ;;
    --qa-only) QA_ONLY="$2"; shift 2 ;;
    --max-retries) MAX_RETRIES="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

[ -z "$PHASE" ] && { echo "ERROR: --phase required"; exit 1; }
[ -z "$DIFF_FILES" ] && { echo "ERROR: --diff required"; exit 1; }

PHASE_SLUG=$(echo "$PHASE" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')
ARTIFACT_DIR="${RALPH_DIR}/${PHASE_SLUG}"
mkdir -p "$ARTIFACT_DIR"

should_run() {
  local dim="$1"
  if [ -n "$QA_ONLY" ]; then
    echo ",$QA_ONLY," | grep -q ",$dim," && return 0 || return 1
  fi
  if [ -n "$QA_SKIP" ]; then
    echo ",$QA_SKIP," | grep -q ",$dim," && return 1 || return 0
  fi
  return 0
}

RESULTS_FILE="$ARTIFACT_DIR/results.json"
echo '{}' > "$RESULTS_FILE"

# --- Deterministic hooks (no LLM, $0) ---

# qa-unit: vitest/pytest on changed files
if should_run "unit"; then
  echo "[RALPH] Running qa-unit (deterministic)..."
  UNIT_STATUS="pass"

  # TypeScript files → vitest
  TS_FILES=$(echo "$DIFF_FILES" | tr ' ' '\n' | grep -E '\.(ts|tsx|js|jsx)$' || true)
  if [ -n "$TS_FILES" ] && command -v npx >/dev/null 2>&1; then
    if npx vitest run --reporter=verbose 2>"$ARTIFACT_DIR/unit-ts.log" | tee -a "$ARTIFACT_DIR/unit-ts.log"; then
      echo "[RALPH] qa-unit (vitest): PASS"
    else
      UNIT_STATUS="fail"
      echo "[RALPH] qa-unit (vitest): FAIL — see $ARTIFACT_DIR/unit-ts.log"
    fi
  fi

  # Python files → pytest
  PY_FILES=$(echo "$DIFF_FILES" | tr ' ' '\n' | grep -E '\.py$' || true)
  if [ -n "$PY_FILES" ] && command -v pytest >/dev/null 2>&1; then
    if pytest -x --tb=short 2>"$ARTIFACT_DIR/unit-py.log" | tee -a "$ARTIFACT_DIR/unit-py.log"; then
      echo "[RALPH] qa-unit (pytest): PASS"
    else
      UNIT_STATUS="fail"
      echo "[RALPH] qa-unit (pytest): FAIL — see $ARTIFACT_DIR/unit-py.log"
    fi
  fi

  # Neither vitest nor pytest available
  if [ -z "$TS_FILES" ] && [ -z "$PY_FILES" ]; then
    UNIT_STATUS="skip"
    echo "[RALPH] qa-unit: SKIP (no testable files in diff)"
  fi

  # Write result — use printf to avoid injection
  printf '{"unit":"%s"}' "$UNIT_STATUS" > "$ARTIFACT_DIR/unit-result.json"
else
  echo "[RALPH] qa-unit: SKIP (--qa-skip or --qa-only)"
  printf '{"unit":"skip"}' > "$ARTIFACT_DIR/unit-result.json"
fi

# qa-integration: API contract tests
if should_run "integration"; then
  echo "[RALPH] Running qa-integration (deterministic)..."
  INT_STATUS="pass"

  API_FILES=$(echo "$DIFF_FILES" | tr ' ' '\n' | grep -E 'api/' || true)
  if [ -n "$API_FILES" ] && command -v npx >/dev/null 2>&1; then
    if npx vitest run --reporter=verbose -- "tests/api" 2>"$ARTIFACT_DIR/integration.log" | tee -a "$ARTIFACT_DIR/integration.log"; then
      echo "[RALPH] qa-integration: PASS"
    else
      INT_STATUS="fail"
      echo "[RALPH] qa-integration: FAIL — see $ARTIFACT_DIR/integration.log"
    fi
  else
    INT_STATUS="skip"
    echo "[RALPH] qa-integration: SKIP (no API files in diff)"
  fi

  printf '{"integration":"%s"}' "$INT_STATUS" > "$ARTIFACT_DIR/integration-result.json"
else
  echo "[RALPH] qa-integration: SKIP (--qa-skip or --qa-only)"
  printf '{"integration":"skip"}' > "$ARTIFACT_DIR/integration-result.json"
fi

# --- LLM agents (via ruflo swarm) ---
# These print their own [RALPH] status lines
# Each reads its prompt from prompts/qa-*.md

SWARM_RESULTS=""
LLM_DIMS=("e2e" "review" "security")

for dim in "${LLM_DIMS[@]}"; do
  if should_run "$dim"; then
    PROMPT_FILE="prompts/qa-${dim}.md"
    if [ ! -f "$PROMPT_FILE" ]; then
      echo "[RALPH] qa-${dim}: SKIP (prompt file missing: $PROMPT_FILE)"
      printf '{"'$dim'":"skip"}' > "$ARTIFACT_DIR/${dim}-result.json"
      continue
    fi

    # qa-e2e: check dev server first
    if [ "$dim" = "e2e" ]; then
      if ! curl -sf http://localhost:3000 -o /dev/null 2>/dev/null; then
        echo "[RALPH] qa-e2e: SKIP (no dev server at localhost:3000)"
        printf '{"e2e":"skip"}' > "$ARTIFACT_DIR/e2e-result.json"
        continue
      fi
    fi

    echo "[RALPH] Spawning qa-${dim} (LLM, sonnet)..."
    # The actual ruflo spawn happens in the calling context (feature-implement)
    # This script outputs the dimension name and prompt path for the orchestrator
    echo "SPAWN:${dim}:${PROMPT_FILE}:${ARTIFACT_DIR}" >> "$ARTIFACT_DIR/spawn-manifest.txt"
  else
    echo "[RALPH] qa-${dim}: SKIP (--qa-skip or --qa-only)"
    printf '{"'$dim'":"skip"}' > "$ARTIFACT_DIR/${dim}-result.json"
  fi
done

# --- Aggregate results ---
echo ""
echo "[RALPH] === Phase QA Summary: $PHASE ==="

OVERALL="pass"
for f in "$ARTIFACT_DIR"/*-result.json; do
  [ -f "$f" ] || continue
  DIM=$(basename "$f" | sed 's/-result.json//')
  # Safe read — no eval
  STATUS=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(list(d.values())[0])" "$f" 2>/dev/null || echo "error")
  printf "  %-15s %s\n" "$DIM:" "$STATUS"
  if [ "$STATUS" = "fail" ]; then
    OVERALL="fail"
  fi
done

echo ""
if [ "$OVERALL" = "pass" ]; then
  echo "[RALPH] Phase QA: PASS"
  exit 0
else
  echo "[RALPH] Phase QA: FAIL"
  echo "[RALPH] Artifacts: $ARTIFACT_DIR/"
  exit 1
fi
