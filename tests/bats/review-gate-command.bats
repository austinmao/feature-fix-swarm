#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/review-gate-command.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  CWD="$BATS_TEST_TMPDIR/cwd"
  mkdir -p "$STUB_DIR" "$CWD"
  export FFS_ADVERSARY_MODEL_PROBE=off

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

@test "Claude-hosted ship review resolves the Codex judgment tier" {
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  grep -F 'model="gpt-5.6-sol"' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'model_reasoning_effort="high"' "$BATS_TEST_TMPDIR/codex.args"
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
  grep -Fx 'claude-opus-5' "$BATS_TEST_TMPDIR/claude.args"
}

@test "opposite reviewer unavailable falls back once to active host with explicit degradation" {
  cat > "$STUB_DIR/opposite-unavailable" <<'EOF'
#!/usr/bin/env bash
exit 127
EOF
  chmod +x "$STUB_DIR/opposite-unavailable"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=opposite-unavailable ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *'DEGRADED'* ]]
  [[ "$output" == *'active-host fallback'* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "both reviewers unavailable fails closed with REVISE" {
  cat > "$STUB_DIR/unavailable" <<'EOF'
#!/usr/bin/env bash
exit 127
EOF
  chmod +x "$STUB_DIR/unavailable"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=unavailable ADVERSARY_BIN_CLAUDE=unavailable \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [[ "$output" == *'both review hosts unavailable'* ]]
}

@test "reviewer rc zero without a final anchored verdict fails closed" {
  cat > "$STUB_DIR/no-verdict" <<'EOF'
#!/usr/bin/env bash
echo 'Looks good; VERDICT: PASS appears only inline.'
exit 0
EOF
  chmod +x "$STUB_DIR/no-verdict"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=no-verdict ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [[ "$output" == *'missing final anchored verdict'* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "one anchored PASS remains valid when the CLI appends a diagnostic" {
  cat > "$STUB_DIR/trailing-diagnostic" <<'EOF'
#!/usr/bin/env bash
echo 'VERDICT: PASS'
echo 'tokens used: 123' >&2
exit 0
EOF
  chmod +x "$STUB_DIR/trailing-diagnostic"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=trailing-diagnostic ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
}

@test "one anchored BLOCK with trailing whitespace fails closed" {
  cat > "$STUB_DIR/block-verdict" <<'EOF'
#!/usr/bin/env bash
printf 'HIGH: data loss risk\nVERDICT: BLOCK   \n'
EOF
  chmod +x "$STUB_DIR/block-verdict"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=block-verdict ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [[ "$output" == *'data loss risk'* ]]
}

@test "multiple anchored verdicts fail closed" {
  cat > "$STUB_DIR/multiple-verdicts" <<'EOF'
#!/usr/bin/env bash
printf 'VERDICT: BLOCK\nVERDICT: PASS\n'
EOF
  chmod +x "$STUB_DIR/multiple-verdicts"

  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=multiple-verdicts ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"

  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [[ "$output" == *'multiple verdicts'* ]]
}

@test "two hung review hosts share one overall ship-review deadline" {
  cat > "$STUB_DIR/hung-codex" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/hung-codex-started"
sleep 30
EOF
  cat > "$STUB_DIR/hung-claude" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/hung-claude-started"
sleep 30
EOF
  chmod +x "$STUB_DIR/hung-codex" "$STUB_DIR/hung-claude"

  start="$(date +%s)"
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    GSD_REVIEW_TIMEOUT=6 RUN_BOUNDED_KILL_AFTER=1 \
    ADVERSARY_BIN_CODEX=hung-codex ADVERSARY_BIN_CLAUDE=hung-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT'"
  elapsed=$(( $(date +%s) - start ))

  [ "$status" -ne 0 ]
  [ -f "$BATS_TEST_TMPDIR/hung-codex-started" ]
  [ -f "$BATS_TEST_TMPDIR/hung-claude-started" ]
  [ "$elapsed" -lt 9 ]
  [[ "$output" == *'both review hosts unavailable'* ]]
}

@test "scope-drift advisory stays on stderr — stdout remains pure JSON verdict" {
  mkdir -p "$CWD/.planning/phases/01-x"
  cat > "$CWD/.planning/phases/01-x/01-01-PLAN.md" <<'EOF'
---
files_modified:
  - a
goal: stdout purity
---
EOF
  run env HOME="$BATS_TEST_TMPDIR" FFS_HOST=claude GSD_RUN_ID=spec-000 \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$CWD' && printf 'diff --git a/a b/a\n' | bash '$SCRIPT' 2>/dev/null"
  [ "$status" -eq 0 ]
  # stdout alone must be exactly the JSON verdict — parseable, no drift lines
  echo "$output" | python3 -m json.tool >/dev/null
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
}
