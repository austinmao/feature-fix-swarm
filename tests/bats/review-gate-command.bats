#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/review-gate-command.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  CWD="$BATS_TEST_TMPDIR/cwd"
  mkdir -p "$STUB_DIR" "$CWD"

  cat > "$STUB_DIR/fake-codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/codex.args"
echo 'VERDICT: PASS'
EOF
  cat > "$STUB_DIR/fake-claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/claude.args"
echo 'VERDICT: PASS'
EOF
  chmod +x "$STUB_DIR/fake-codex" "$STUB_DIR/fake-claude"
  export PATH="$STUB_DIR:$PATH"
}

@test "Claude-hosted ship review uses Codex Sol xhigh" {
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  grep -F 'model="gpt-5.6-sol"' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'model_reasoning_effort="xhigh"' "$BATS_TEST_TMPDIR/codex.args"
}

@test "Codex-hosted ship review uses Claude Opus" {
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=codex GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  grep -Fx -- '--model' "$BATS_TEST_TMPDIR/claude.args"
  grep -Fx 'opus' "$BATS_TEST_TMPDIR/claude.args"
}

