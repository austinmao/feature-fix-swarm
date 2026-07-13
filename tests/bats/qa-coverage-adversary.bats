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
  cat > "$STUB_DIR/fake-claude-adequate" <<'EOF'
#!/usr/bin/env bash
echo "COVERAGE: ADEQUATE"
EOF
  chmod +x "$STUB_DIR/fake-claude-adequate"
  export PATH="$STUB_DIR:$PATH"
  export FFS_HOST=claude
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

@test "unknown flag is usage error exit 2" {
  run bash "$SCRIPT" "$RESULTS" --bogus-flag
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: qa-coverage-adversary.sh"* ]]
}

@test "missing adversary CLI is fail-soft exit 0" {
  QA_COVERAGE_BIN=definitely-not-a-real-cli \
    QA_COVERAGE_FALLBACK_BIN=also-not-a-real-cli run bash "$SCRIPT" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"both review hosts unavailable — skipped (advisory)"* ]]
}

@test "missing preferred QA reviewer falls back once to active host" {
  QA_COVERAGE_BIN=definitely-not-a-real-cli \
    QA_COVERAGE_FALLBACK_BIN=fake-claude-adequate run bash "$SCRIPT" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DEGRADED"* ]]
  [[ "$output" == *"COVERAGE: ADEQUATE"* ]]
}

@test "adversary CLI exit 7 is fail-soft exit 0" {
  cat > "$STUB_DIR/fake-codex-fail" <<'EOF'
#!/usr/bin/env bash
exit 7
EOF
  chmod +x "$STUB_DIR/fake-codex-fail"
  QA_COVERAGE_BIN=fake-codex-fail QA_COVERAGE_FALLBACK_BIN=definitely-missing \
    run bash "$SCRIPT" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"both review hosts unavailable — skipped (advisory)"* ]]
}

@test "stub output with neither MISSED nor ADEQUATE prints no-findings fallback" {
  cat > "$STUB_DIR/fake-codex-blank" <<'EOF'
#!/usr/bin/env bash
echo "some unstructured commentary with no anchored tags"
EOF
  chmod +x "$STUB_DIR/fake-codex-blank"
  QA_COVERAGE_BIN=fake-codex-blank run bash "$SCRIPT" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"no parseable coverage findings"* ]]
}

@test "--diff-base as last arg (no value) is usage error exit 2, no hang" {
  # timeout guards the suite: pre-fix this spins forever (shift 2 never consumes)
  TIMEOUT_BIN="$(command -v timeout || command -v gtimeout)"
  run "$TIMEOUT_BIN" 10 bash "$SCRIPT" "$RESULTS" --diff-base
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: qa-coverage-adversary.sh"* ]]
}

@test "explicit --diff-base <sha> works" {
  BASE_SHA="$(git rev-parse HEAD)"
  QA_COVERAGE_BIN=fake-codex-adequate run bash "$SCRIPT" "$RESULTS" --diff-base "$BASE_SHA"
  [ "$status" -eq 0 ]
  [[ "$output" == *"COVERAGE: ADEQUATE"* ]]
}
