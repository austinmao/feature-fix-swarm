#!/usr/bin/env bats
# Tests for qa-swarm.sh v3.20.0 changes: browser-proof integration
# (e2e fail-not-skip), conditional design dimension, and --aggregate mode
# with runtime-proof enforcement (self-reported "pass" without a verified
# proof bundle is REJECTED at aggregation).

SCRIPT="$BATS_TEST_DIRNAME/qa-swarm.sh"

setup() {
  export WORK="$BATS_TMPDIR/qsw-$$"
  mkdir -p "$WORK/prompts"
  # prompt stubs so LLM dims queue instead of skipping on missing file
  for d in e2e review security design; do
    echo "# stub" > "$WORK/prompts/qa-$d.md"
  done
  cd "$WORK"
  unset QA_BASE_URL QA_ALLOW_NO_SERVER
  export BROWSER_PROOF_PROBE_PORTS="59899"   # nothing listens
  export ART=".ralph/phase-9"
}

teardown() {
  cd /
  rm -rf "$WORK"
}

write_all_skip_results() {
  mkdir -p "$ART"
  for d in unit integration e2e review security design; do
    printf '{"%s":"skip"}' "$d" > "$ART/${d}-result.json"
  done
}

make_valid_proof() { # $1 = output proof path
  local shot="$WORK/shot.png"
  printf 'PNGDATA' > "$shot"
  python3 - "$1" "$shot" <<'EOF'
import json, sys
proof = {"version": 1, "driver": "playwright", "base_url": "http://x",
         "scenarios": [{"id": "S1", "kind": "functional", "status": "pass",
                        "url": "/", "url_final": "http://x/", "expect_url": "/",
                        "http_status": 200, "content_assert": "h1",
                        "dom_excerpt": "<h1>Home</h1>", "console_errors": [],
                        "interactions": 1, "screenshot": sys.argv[2]}]}
json.dump(proof, open(sys.argv[1], "w"))
EOF
}

# ------------------------------------------------- e2e fail-not-skip

@test "web-touching diff with no server: e2e FAILS, not skips" {
  run bash "$SCRIPT" --phase "Phase 9" --diff "web/src/app/page.tsx" \
      --spec-dir specs/999 --qa-only e2e
  [ "$status" -eq 1 ]
  grep -q '"e2e":"fail"' "$ART/e2e-result.json"
  [[ "$output" == *"fail-not-skip"* ]]
}

@test "non-web diff: e2e skips legitimately" {
  run bash "$SCRIPT" --phase "Phase 9" --diff "lib/gates.py" \
      --spec-dir specs/999 --qa-only e2e
  [ "$status" -eq 0 ]
  grep -q '"e2e":"skip"' "$ART/e2e-result.json"
}

@test "explicit waiver skips with WAIVED marker" {
  export QA_ALLOW_NO_SERVER=1
  run bash "$SCRIPT" --phase "Phase 9" --diff "web/src/app/page.tsx" \
      --spec-dir specs/999 --qa-only e2e
  [ "$status" -eq 0 ]
  grep -q '"e2e":"skip"' "$ART/e2e-result.json"
  [[ "$output" == *"WAIVED"* ]]
}

# ------------------------------------------------- design dimension

@test "UI diff queues design dimension" {
  export QA_ALLOW_NO_SERVER=1
  run bash "$SCRIPT" --phase "Phase 9" --diff "web/src/components/Nav.tsx" \
      --spec-dir specs/999 --qa-only design
  [[ "$output" == *"Queueing qa-design"* ]]
  grep -q "SPAWN:design:" "$ART/spawn-manifest.txt"
}

@test "non-UI diff skips design dimension" {
  run bash "$SCRIPT" --phase "Phase 9" --diff "lib/gates.py scripts/x.sh" \
      --spec-dir specs/999 --qa-only design
  [ "$status" -eq 0 ]
  grep -q '"design":"skip"' "$ART/design-result.json"
}

# ------------------------------------------------- aggregate mode

@test "aggregate: all-skip results pass" {
  write_all_skip_results
  run bash "$SCRIPT" --phase "Phase 9" --diff "x" --spec-dir specs/999 --aggregate
  [ "$status" -eq 0 ]
}

@test "aggregate: e2e self-reported pass WITHOUT proof bundle is rejected" {
  write_all_skip_results
  printf '{"e2e":"pass"}' > "$ART/e2e-result.json"
  run bash "$SCRIPT" --phase "Phase 9" --diff "x" --spec-dir specs/999 --aggregate
  [ "$status" -eq 1 ]
  [[ "$output" == *"REJECTED"* ]]
}

@test "aggregate: e2e pass WITH verified proof bundle passes" {
  write_all_skip_results
  printf '{"e2e":"pass"}' > "$ART/e2e-result.json"
  make_valid_proof "$ART/proof.json"
  run bash "$SCRIPT" --phase "Phase 9" --diff "x" --spec-dir specs/999 --aggregate
  [ "$status" -eq 0 ]
}

@test "aggregate: design self-reported pass WITHOUT design-proof is rejected" {
  write_all_skip_results
  printf '{"design":"pass"}' > "$ART/design-result.json"
  run bash "$SCRIPT" --phase "Phase 9" --diff "x" --spec-dir specs/999 --aggregate
  [ "$status" -eq 1 ]
  [[ "$output" == *"REJECTED"* ]]
}

@test "aggregate: does not reset pending results back to manifest state" {
  write_all_skip_results
  printf '{"review":"fail"}' > "$ART/review-result.json"
  run bash "$SCRIPT" --phase "Phase 9" --diff "x" --spec-dir specs/999 --aggregate
  [ "$status" -eq 1 ]
  # file untouched — aggregate mode never rewrites dim results
  grep -q '"review":"fail"' "$ART/review-result.json"
}
