#!/usr/bin/env bash
set -euo pipefail

# Ralph Retry Loop — investigate → fix → qa-only until green or max retries
# Called by feature-implement Step 5.5 when qa-swarm.sh exits non-zero.
#
# Usage: bash scripts/ralph-retry.sh \
#   --phase "Phase 2" \
#   --failed-dims "e2e,security" \
#   --spec-dir specs/082-foo \
#   --artifact-dir .ralph/phase-2 \
#   --max-retries 3 \
#   --diff-files "file1.ts file2.py"
#
# Exit codes:
#   0 = all failed dimensions now pass
#   1 = max retries exhausted, still failing

PHASE=""
FAILED_DIMS=""
SPEC_DIR=""
ARTIFACT_DIR=""
MAX_RETRIES=3
DIFF_FILES=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2 ;;
    --failed-dims) FAILED_DIMS="$2"; shift 2 ;;
    --spec-dir) SPEC_DIR="$2"; shift 2 ;;
    --artifact-dir) ARTIFACT_DIR="$2"; shift 2 ;;
    --max-retries) MAX_RETRIES="$2"; shift 2 ;;
    --diff-files) DIFF_FILES="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

[ -z "$PHASE" ] && { echo "ERROR: --phase required"; exit 1; }
[ -z "$FAILED_DIMS" ] && { echo "ERROR: --failed-dims required"; exit 1; }
[ -z "$ARTIFACT_DIR" ] && { echo "ERROR: --artifact-dir required"; exit 1; }

RETRY=0

while [ "$RETRY" -lt "$MAX_RETRIES" ]; do
  RETRY=$((RETRY + 1))
  echo ""
  echo "[RALPH] ===== Retry $RETRY/$MAX_RETRIES for $PHASE ====="
  echo "[RALPH] Failed dimensions: $FAILED_DIMS"
  echo ""

  # Step 1: Capture failure context
  RETRY_DIR="$ARTIFACT_DIR/retry-$RETRY"
  mkdir -p "$RETRY_DIR"

  # Collect failure logs from the QA swarm's artifact dir
  for dim in $(echo "$FAILED_DIMS" | tr ',' ' '); do
    LOG_FILE="$ARTIFACT_DIR/${dim}-result.json"
    if [ -f "$LOG_FILE" ]; then
      cp -- "$LOG_FILE" "$RETRY_DIR/${dim}-prior.json"
    fi
    # Copy any test output logs
    for log in "$ARTIFACT_DIR"/*"${dim}"*.log; do
      [ -f "$log" ] && cp -- "$log" "$RETRY_DIR/" 2>/dev/null || true
    done
  done

  # Step 2: Build investigation context
  # The investigate skill will be invoked by the parent (feature-implement)
  # with scope-lock to changed files only. We produce the context file here.
  CONTEXT_FILE="$RETRY_DIR/investigate-context.md"
  cat > "$CONTEXT_FILE" <<CTXEOF
# Ralph Retry Context — $PHASE, Retry $RETRY/$MAX_RETRIES

## Failed QA dimensions
$(for dim in $(echo "$FAILED_DIMS" | tr ',' ' '); do
    echo "- **$dim**"
    RESULT="$ARTIFACT_DIR/${dim}-result.json"
    if [ -f "$RESULT" ]; then
      echo '  ```json'
      cat -- "$RESULT"
      echo '  ```'
    fi
  done)

## Changed files in this phase
$(echo "$DIFF_FILES" | tr ' ' '\n' | sed 's/^/- /')

## Spec directory
$SPEC_DIR

## What to investigate
Use the 5 Whys approach. Scope-lock to the files listed above.
Do NOT modify files outside the changed-files list.
CTXEOF

  echo "[RALPH] Investigation context written to $CONTEXT_FILE"

  # Step 3: Signal the parent to invoke /investigate
  # The parent (feature-implement) reads this file to know what to investigate
  echo "INVESTIGATE:$RETRY_DIR:$CONTEXT_FILE:$FAILED_DIMS" > "$ARTIFACT_DIR/retry-signal.txt"

  # Step 4: Wait for the parent to signal that fixes are applied
  # The parent writes "FIXED" to this file after the investigate+fix sub-agent completes
  FIXED_SIGNAL="$RETRY_DIR/fixed-signal.txt"
  echo "[RALPH] Waiting for fix to be applied (parent will write to $FIXED_SIGNAL)..."

  # In practice, this script is called synchronously by the SKILL.md orchestrator,
  # which handles the investigate → fix cycle. The retry loop is conceptual —
  # the actual retry happens in the SKILL.md workflow.
  #
  # For standalone testing, we check if fixed-signal exists (pre-created by test harness)
  if [ -f "$FIXED_SIGNAL" ]; then
    echo "[RALPH] Fix signal received"
  else
    echo "[RALPH] No fix signal — parent orchestrator will handle investigate+fix cycle"
    echo "[RALPH] Exiting with retry context at $RETRY_DIR"
    # Write the retry state so the parent can resume
    printf '{"retry":%d,"max":%d,"phase":"%s","failed_dims":"%s","context":"%s"}' \
      "$RETRY" "$MAX_RETRIES" "$PHASE" "$FAILED_DIMS" "$CONTEXT_FILE" \
      > "$ARTIFACT_DIR/retry-state.json"
    exit 1
  fi

  # Step 5: Re-run QA on failed dimensions only (qa-only scope)
  echo "[RALPH] Re-running QA on failed dimensions: $FAILED_DIMS"

  QA_RERUN_EXIT=0
  if bash scripts/qa-swarm.sh \
    --phase "$PHASE" \
    --diff "$DIFF_FILES" \
    --spec-dir "${SPEC_DIR:-specs/unknown}" \
    --qa-only "$FAILED_DIMS"; then
    echo "[RALPH] Retry $RETRY/$MAX_RETRIES: ALL PASS"
    # Clean up retry state
    rm -f "$ARTIFACT_DIR/retry-signal.txt" "$ARTIFACT_DIR/retry-state.json" 2>/dev/null
    exit 0
  else
    echo "[RALPH] Retry $RETRY/$MAX_RETRIES: Still failing"
    # Update failed dims to only those still failing
    NEW_FAILED=""
    for dim in $(echo "$FAILED_DIMS" | tr ',' ' '); do
      RESULT="$ARTIFACT_DIR/${dim}-result.json"
      if [ -f "$RESULT" ]; then
        STATUS=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(list(d.values())[0])" "$RESULT" 2>/dev/null || echo "error")
        if [ "$STATUS" = "fail" ] || [ "$STATUS" = "error" ]; then
          [ -n "$NEW_FAILED" ] && NEW_FAILED="${NEW_FAILED},"
          NEW_FAILED="${NEW_FAILED}${dim}"
        fi
      fi
    done
    FAILED_DIMS="${NEW_FAILED:-$FAILED_DIMS}"
    echo "[RALPH] Still failing: $FAILED_DIMS"
  fi
done

echo ""
echo "[RALPH] ===== MAX RETRIES EXHAUSTED ($MAX_RETRIES) ====="
echo "[RALPH] Phase: $PHASE"
echo "[RALPH] Still failing: $FAILED_DIMS"
echo "[RALPH] Artifacts: $ARTIFACT_DIR/"
echo "[RALPH] Resume: /feature-implement <NNN> --resume"

# Write final state
printf '{"retry":%d,"max":%d,"phase":"%s","failed_dims":"%s","exhausted":true}' \
  "$MAX_RETRIES" "$MAX_RETRIES" "$PHASE" "$FAILED_DIMS" \
  > "$ARTIFACT_DIR/retry-state.json"

exit 1
