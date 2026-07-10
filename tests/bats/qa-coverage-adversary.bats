#!/usr/bin/env bats
# qa-coverage-adversary.sh — advisory (never blocks) cross-model QA
# coverage critique: what user-facing flows did the recorded QA run miss
# vs. the code diff. Same host-aware adversary lib as plan-adversary.sh.

SCRIPT="scripts/gsd/qa-coverage-adversary.sh"

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  cat > "$RESULTS" <<'EOF'
{"steps": [{"name": "login flow"}, {"name": "checkout"}]}
EOF
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  # Default kind resolves to codex (bats runs under a claude host, no
  # PLAN/QA *_KIND override) — same convention as plan-adversary.bats.
  cat > "$STUB_DIR/fake-codex-missed" <<'EOF'
#!/usr/bin/env bash
echo "echoing prompt: List user-facing flows the QA MISSED"
echo "MISSED: password reset flow — no coverage of expired token path"
echo "MISSED: billing downgrade — unverified refund handling"
EOF
  chmod +x "$STUB_DIR/fake-codex-missed"
  cat > "$STUB_DIR/fake-codex-adequate" <<'EOF'
#!/usr/bin/env bash
echo "echoing prompt: List user-facing flows the QA MISSED"
echo "COVERAGE: ADEQUATE"
EOF
  chmod +x "$STUB_DIR/fake-codex-adequate"
  export PATH="$STUB_DIR:$PATH"
}

@test "kill-switch QA_COVERAGE=off skips" {
  QA_COVERAGE=off run bash "$SCRIPT" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"disabled"* ]]
}

@test "missing results file is fail-soft exit 0" {
  run bash "$SCRIPT" "$BATS_TEST_TMPDIR/does-not-exist.json"
  [ "$status" -eq 0 ]
  [[ "$output" == *"not found — skipped (fail-soft)"* ]]
}

@test "stub emitting MISSED lines prints them, not the prompt echo" {
  QA_COVERAGE_BIN=fake-codex-missed run bash "$SCRIPT" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MISSED: password reset flow — no coverage of expired token path"* ]]
  [[ "$output" == *"MISSED: billing downgrade — unverified refund handling"* ]]
  [[ "$output" != *"echoing prompt"* ]]
}

@test "stub emitting ADEQUATE prints coverage-adequate verdict" {
  QA_COVERAGE_BIN=fake-codex-adequate run bash "$SCRIPT" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COVERAGE: ADEQUATE"* ]]
}

@test "missing arg is usage error exit 2" {
  run bash "$SCRIPT"
  [ "$status" -eq 2 ]
}
