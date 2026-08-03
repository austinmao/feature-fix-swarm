#!/usr/bin/env bash
# spec-panel-eval-fixture.sh — spec-004 EVAL-D fixture harness (Phase 4).
# Runs scripts/gsd/spec-panel.sh under SPEC_PANEL=on against STUBBED vendor
# CLIs (no live model calls — mirrors the tests/bats/adversary-host.bats
# stub convention) across three arms — panel, degrade-anthropic,
# degrade-openai — scores each resulting synthesis against the AC-010
# rubric (Coverage / Assumptions / Failure modes / Testability), and writes
# the fixture-run record to evals/spec-panel/eval-d-results.json, honestly
# labeled stub-mode.
#
# Building this measurement is the Phase 4 done-condition (plan.md "Phase 4
# — spec panel"); a pass does NOT flip SPEC_PANEL's default — AC-010
# reserves that for a follow-up change. This script never sets SPEC_PANEL
# for any caller other than its own stubbed subprocesses.
#
# Usage: spec-panel-eval-fixture.sh [output-json-path]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT="${1:-$REPO_ROOT/evals/spec-panel/eval-d-results.json}"
mkdir -p "$(dirname "$OUT")"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/spec-panel-eval.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

BRIEF="$WORK/brief.txt"
cat > "$BRIEF" <<'EOF'
Fixture brief: add a bounded retry to a flaky network call in the ingest
pipeline. State what "done" looks like.
EOF

STUB_DIR="$WORK/bin"
mkdir -p "$STUB_DIR"
# One stub plays both vendors: adversary_invoke's codex branch always passes
# -o/--output-last-message (consumed here); its claude branch never does, so
# the else-branch stdout print is what adversary_invoke captures for claude.
# The prompt itself (stdin) — not argv, not the caller's kind — selects
# draft vs refute content, since spec-panel.sh's refute prompt is the only
# one containing "You are refuting".
cat > "$STUB_DIR/stub-vendor" <<'STUB'
#!/usr/bin/env bash
last_message=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|--output-last-message) last_message="$2"; shift 2 ;;
    *) shift ;;
  esac
done
input="$(cat)"
body='## Coverage

Covers the primary flow end to end.

## Assumptions

Assumes the brief input is well-formed.

## Failure modes

Names the empty-input case as a failure mode.

## Testability

One deterministic check per branch.'
case "$input" in
  *"You are refuting"*)
    body='## Coverage

Add: also cover the retry path.

## Assumptions

Add: assumes network access is available.

## Failure modes

Add: name the timeout failure mode.

## Testability

Add: one test for the retry path.'
    ;;
esac
if [ -n "$last_message" ]; then
  printf '%s\n' "$body" > "$last_message"
else
  printf '%s\n' "$body"
fi
STUB
chmod +x "$STUB_DIR/stub-vendor"

# _rubric_axes_passed <synthesis-file>
# Counts how many of the four fixed axes carry non-empty grafted content.
_rubric_axes_passed() {
  local f="$1" n=0 axis
  for axis in Coverage Assumptions "Failure modes" Testability; do
    if awk -v h="## $axis" '
      $0 == h { found=1; next }
      found && /^## / { found=0 }
      found && NF { seen=1 }
      END { exit !seen }
    ' "$f"; then
      n=$((n + 1))
    fi
  done
  printf '%s' "$n"
}

_run_arm() {
  local arm="$1" claude_bin="$2" codex_bin="$3"
  local spec_dir="$WORK/arm-$arm"
  mkdir -p "$spec_dir"
  SPEC_PANEL=on SPEC_PANEL_TIMEOUT=30 \
    ADVERSARY_BIN_CLAUDE="$claude_bin" ADVERSARY_BIN_CODEX="$codex_bin" \
    PATH="$STUB_DIR:$PATH" \
    "$SCRIPT_DIR/spec-panel.sh" "$BRIEF" "$spec_dir" >"$WORK/$arm.log" 2>&1
  echo $?
}

panel_rc="$(_run_arm panel stub-vendor stub-vendor)"
degrade_a_rc="$(_run_arm degrade-anthropic stub-vendor no-such-codex-xyz)"
degrade_o_rc="$(_run_arm degrade-openai no-such-claude-xyz stub-vendor)"

if [ "$panel_rc" != 0 ] || [ "$degrade_a_rc" != 0 ] || [ "$degrade_o_rc" != 0 ]; then
  echo "spec-panel-eval-fixture: an arm failed (panel=$panel_rc degrade-anthropic=$degrade_a_rc degrade-openai=$degrade_o_rc)" >&2
  cat "$WORK/panel.log" "$WORK/degrade-anthropic.log" "$WORK/degrade-openai.log" >&2
  exit 1
fi

panel_axes="$(_rubric_axes_passed "$WORK/arm-panel/panel/synthesis.md")"
degrade_a_axes="$(_rubric_axes_passed "$WORK/arm-degrade-anthropic/panel/synthesis.md")"
degrade_o_axes="$(_rubric_axes_passed "$WORK/arm-degrade-openai/panel/synthesis.md")"
baseline_axes="$degrade_a_axes"
[ "$degrade_o_axes" -lt "$baseline_axes" ] && baseline_axes="$degrade_o_axes"

eval_d_pass=false
[ "$panel_axes" -ge 3 ] && eval_d_pass=true

jq -n \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson panel_axes "$panel_axes" \
  --argjson degrade_a_axes "$degrade_a_axes" \
  --argjson degrade_o_axes "$degrade_o_axes" \
  --argjson baseline_axes "$baseline_axes" \
  --argjson eval_d_pass "$eval_d_pass" \
  '{
    schema: "ffs.eval-d/v1",
    mode: "stub",
    note: "stub-mode: vendor CLIs replaced by a canned fixture stub (scripts/gsd/spec-panel-eval-fixture.sh); no live model calls were made. Fixture-run output only, not a live-model judgment.",
    generated_at: $generated_at,
    rubric_axes: ["Coverage", "Assumptions", "Failure modes", "Testability"],
    arms: [
      {arm: "panel", relation: "cross-vendor", axes_passed: $panel_axes, axes_total: 4, pass: ($panel_axes >= 3)},
      {arm: "degrade-anthropic", relation: "self", axes_passed: $degrade_a_axes, axes_total: 4, pass: ($degrade_a_axes >= 3)},
      {arm: "degrade-openai", relation: "self", axes_passed: $degrade_o_axes, axes_total: 4, pass: ($degrade_o_axes >= 3)}
    ],
    eval_d: {
      definition: "AC-010: EVAL-D pass = panel >=3-of-4 rubric axes over the author+refuter degrade baseline",
      panel_axes_passed: $panel_axes,
      baseline_axes_passed: $baseline_axes,
      pass: $eval_d_pass,
      authorizes_default_flip: "a pass authorizes flipping SPEC_PANEL'\''s default in a FOLLOW-UP change; nothing flips automatically (AC-010)"
    }
  }' > "$OUT"

echo "spec-panel-eval-fixture: wrote $OUT"
