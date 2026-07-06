#!/usr/bin/env bash
set -euo pipefail

# QA Swarm Manifest Builder — queues 3 LLM QA prompts + runs 2 deterministic hooks per phase
# Usage: bash scripts/qa-swarm.sh --phase "Phase 2" --diff "file1.ts file2.py" --spec-dir specs/082-foo [--qa-skip e2e] [--qa-only review,security] [--max-retries 3]

PHASE=""
DIFF_FILES=""
SPEC_DIR=""
QA_SKIP=""
QA_ONLY=""
MAX_RETRIES=3
RALPH_DIR=".ralph"
AGGREGATE=0
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2 ;;
    --diff) DIFF_FILES="$2"; shift 2 ;;
    --spec-dir) SPEC_DIR="$2"; shift 2 ;;
    --qa-skip) QA_SKIP="$2"; shift 2 ;;
    --qa-only) QA_ONLY="$2"; shift 2 ;;
    --max-retries) MAX_RETRIES="$2"; shift 2 ;;
    --aggregate) AGGREGATE=1; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

[ -z "$PHASE" ] && { echo "ERROR: --phase required"; exit 1; }
[ -z "$DIFF_FILES" ] && { echo "ERROR: --diff required"; exit 1; }
: "$SPEC_DIR" "$MAX_RETRIES"

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
LLM_DIMS=("e2e" "review" "security" "design")

# --aggregate: re-read existing ${dim}-result.json and enforce proof bundles,
# WITHOUT re-running hooks or resetting LLM results back to "pending". The
# orchestrator calls this after the QA agents have written their verdicts.
if [ "$AGGREGATE" != "1" ]; then

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

# --- LLM agents (Ruflo-coordinated, host-CLI executed by feature-implement) ---
# These print their own [RALPH] status lines
# Each reads its prompt from prompts/qa-*.md

# qa-design trigger: only when the phase's diff touches visual surfaces.
# (The old behavior gated design review on /design-html tasks only — ordinary
# component/page/CSS work shipped with zero design review.)
UI_RE='\.(tsx|jsx|vue|svelte|astro|html|css|scss|less)$|(^|/)(emails|templates|styles?)/|(^|/)tailwind\.config\.'
DESIGN_TRIGGER="no"
for f in $DIFF_FILES; do
  if echo "$f" | grep -qE "$UI_RE"; then DESIGN_TRIGGER="yes"; break; fi
done

for dim in "${LLM_DIMS[@]}"; do
  if should_run "$dim"; then
    # qa-e2e: resolve browser context FIRST — a web-touching diff with no
    # reachable app is a FAILURE, never a silent skip (v3.20.0).
    if [ "$dim" = "e2e" ]; then
      BP_RC=0
      BP_OUT=$(bash "$SCRIPT_DIR/browser-proof.sh" --diff "$DIFF_FILES" 2>&1) || BP_RC=$?
      echo "$BP_OUT" > "$ARTIFACT_DIR/browser-proof.txt"
      if echo "$BP_OUT" | grep -q "BROWSER-PROOF: NOT-NEEDED"; then
        echo "[RALPH] qa-e2e: SKIP (no web surfaces in diff)"
        printf '{"e2e":"skip"}' > "$ARTIFACT_DIR/e2e-result.json"
        continue
      fi
      if echo "$BP_OUT" | grep -q "BROWSER-PROOF: WAIVED"; then
        echo "[RALPH] qa-e2e: SKIP — WAIVED explicitly (QA_ALLOW_NO_SERVER=1)"
        printf '{"e2e":"skip"}' > "$ARTIFACT_DIR/e2e-result.json"
        continue
      fi
      if [ "$BP_RC" -ne 0 ]; then
        echo "[RALPH] qa-e2e: FAIL — web-touching diff, no reachable app (fail-not-skip)"
        echo "$BP_OUT" | sed 's/^/[RALPH]   /'
        printf '{"e2e":"fail"}' > "$ARTIFACT_DIR/e2e-result.json"
        continue
      fi
    fi

    # qa-design: only meaningful when visual surfaces changed
    if [ "$dim" = "design" ] && [ "$DESIGN_TRIGGER" = "no" ]; then
      echo "[RALPH] qa-design: SKIP (no visual surfaces in diff)"
      printf '{"design":"skip"}' > "$ARTIFACT_DIR/design-result.json"
      continue
    fi

    PROMPT_FILE="prompts/qa-${dim}.md"
    if [ ! -f "$PROMPT_FILE" ]; then
      echo "[RALPH] qa-${dim}: SKIP (prompt file missing: $PROMPT_FILE)"
      printf '{"'$dim'":"skip"}' > "$ARTIFACT_DIR/${dim}-result.json"
      continue
    fi

    echo "[RALPH] Queueing qa-${dim} (LLM, sonnet)..."
    # The calling context executes each queued prompt via native Task() agents.
    # This script only writes the manifest.
    # codex-gate: B1 - queued LLM checks must not disappear from aggregation.
    printf '{"%s":"pending"}' "$dim" > "$ARTIFACT_DIR/${dim}-result.json"
    echo "SPAWN:${dim}:${PROMPT_FILE}:${ARTIFACT_DIR}" >> "$ARTIFACT_DIR/spawn-manifest.txt"
  else
    echo "[RALPH] qa-${dim}: SKIP (--qa-skip or --qa-only)"
    printf '{"'$dim'":"skip"}' > "$ARTIFACT_DIR/${dim}-result.json"
  fi
done

fi  # end of manifest-building (skipped under --aggregate)

# --- Aggregate results ---
echo ""
echo "[RALPH] === Phase QA Summary: $PHASE ==="

OVERALL="pass"

# Browser/design "pass" verdicts are self-reports until the proof bundle
# survives runtime_proof.py — an LLM agent claiming pass with no verified
# evidence is REJECTED here (curl-200 / wrong-page / soft-404 defenses live
# in the verifier, not in agent prose).
# Resolution covers all three install shapes: canonical repo / vendored
# packages/feature-fix-swarm (../lib), CWD lib/, and the ~/.claude install
# (setup.sh puts runtime_proof.py under $HOME/.claude/lib/feature-fix-swarm/
# while qa-swarm.sh lands in the project's scripts/ — ../lib doesn't exist).
RP="$SCRIPT_DIR/../lib/runtime_proof.py"
[ -f "$RP" ] || RP="lib/runtime_proof.py"
[ -f "$RP" ] || RP="$HOME/.claude/lib/feature-fix-swarm/runtime_proof.py"
VERIFY_ARGS=()
# QA_SCENARIOS=specs/NNN/scenarios.md enforces coverage completeness — a
# bundle proving one easy scenario while scenarios.md lists five is rejected.
[ -n "${QA_SCENARIOS:-}" ] && VERIFY_ARGS+=(--scenarios "$QA_SCENARIOS")
for dim in e2e design; do
  rf="$ARTIFACT_DIR/${dim}-result.json"
  [ -f "$rf" ] || continue
  DSTATUS=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(list(d.values())[0])" "$rf" 2>/dev/null || echo "error")
  if [ "$DSTATUS" = "pass" ]; then
    PROOF="$ARTIFACT_DIR/proof.json"
    KIND="functional"
    if [ "$dim" = "design" ]; then
      PROOF="$ARTIFACT_DIR/design-proof.json"
      KIND="visual"
    fi
    # --kind pins the coverage requirement to SOURCE scenario kinds — a
    # bundle self-declaring everything "visual" can't shrink what proof.json
    # must cover (codex round, CRITICAL)
    if ! python3 "$RP" verify "$PROOF" --kind "$KIND" \
        ${VERIFY_ARGS[@]+"${VERIFY_ARGS[@]}"}; then
      echo "[RALPH] qa-${dim}: self-reported pass REJECTED — proof bundle missing/invalid ($PROOF)"
      # overwrite the result file itself — a stale {"dim":"pass"} must not
      # survive for any downstream reader (adversarial round F5)
      printf '{"%s":"fail"}' "$dim" > "$rf"
      OVERALL="fail"
    fi
  fi
done
for f in "$ARTIFACT_DIR"/*-result.json; do
  [ -f "$f" ] || continue
  DIM=$(basename "$f" | sed 's/-result.json//')
  # Safe read — no eval
  STATUS=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(list(d.values())[0])" "$f" 2>/dev/null || echo "error")
  printf "  %-15s %s\n" "$DIM:" "$STATUS"
  if [ "$STATUS" = "fail" ] || [ "$STATUS" = "pending" ] || [ "$STATUS" = "error" ]; then
    OVERALL="fail"
  fi
done

for dim in "unit" "integration" "${LLM_DIMS[@]}"; do
  if should_run "$dim" && [ ! -f "$ARTIFACT_DIR/${dim}-result.json" ]; then
    printf "  %-15s %s\n" "$dim:" "missing"
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
