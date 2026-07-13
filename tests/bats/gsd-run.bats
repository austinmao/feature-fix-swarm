#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/gsd-run.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR" "$BATS_TEST_TMPDIR/codex-root"

  cat > "$STUB_DIR/fake-codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/codex.args"
echo CODEX_OK
EOF
  cat > "$STUB_DIR/fake-claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/claude.args"
echo CLAUDE_OK
EOF
  chmod +x "$STUB_DIR/fake-codex" "$STUB_DIR/fake-claude"
  export PATH="$STUB_DIR:$PATH"
  export GSD_CODEX_CONFIG_ROOT="$BATS_TEST_TMPDIR/codex-root"
}

@test "Codex host runs Codex with the Sonnet-equivalent Terra lead" {
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'fix the host leak'"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CODEX_OK"* ]]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  grep -Fx 'exec' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'model="gpt-5.6-terra"' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'model_reasoning_effort="high"' "$BATS_TEST_TMPDIR/codex.args"
  grep -F '$gsd-quick fix the host leak' "$BATS_TEST_TMPDIR/codex.args"
}

@test "Claude host runs Claude with the Sonnet lead" {
  FFS_HOST=claude CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'fix the host leak'"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CLAUDE_OK"* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  grep -Fx -- '--model' "$BATS_TEST_TMPDIR/claude.args"
  grep -Fx 'claude-sonnet-5' "$BATS_TEST_TMPDIR/claude.args"
  grep -F '/gsd-quick fix the host leak' "$BATS_TEST_TMPDIR/claude.args"
}

@test "missing native Codex CLI fails instead of crossing to Claude" {
  FFS_HOST=codex CODEX_BIN=definitely-missing-codex CLAUDE_BIN=fake-claude \
    run -127 bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 127 ]
  [[ "$output" == *"Codex CLI not found"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}
