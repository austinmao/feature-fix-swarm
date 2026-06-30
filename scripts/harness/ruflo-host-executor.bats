#!/usr/bin/env bats

# codex-gate spec-205-gate-hardening (FINDING 11): exercise the install source
# shipped by this standalone OSS repo so the harness cannot drift.
REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
HOST_EXECUTOR="$REPO_ROOT/scripts/harness/ruflo-host-executor.sh"

setup() {
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  export PATH="$STUB_DIR:$PATH"

  PROMPT_FILE="$BATS_TEST_TMPDIR/prompt.txt"
  printf 'test prompt\n' > "$PROMPT_FILE"

  TARGET_DIR="$BATS_TEST_TMPDIR/target"
  mkdir -p "$TARGET_DIR"
  TARGET_PWD="$(cd "$TARGET_DIR" && pwd -P)"
  export PROMPT_FILE TARGET_DIR TARGET_PWD

  # codex-gate spec-205-gate-hardening (FINDING 4): the bats target dir lives outside the
  # repo, so for the happy-path assertions point the containment root at the tmpdir.
  export HOST_EXECUTOR_ROOT="$(cd "$BATS_TEST_TMPDIR" && pwd -P)"

  HARNESS_RECORD="$BATS_TEST_TMPDIR/record.txt"
  export HARNESS_RECORD

  cat > "$STUB_DIR/claude" <<'EOF'
#!/usr/bin/env bash
{
  printf 'pwd=%s\n' "$(pwd -P)"
  index=0
  for arg in "$@"; do
    printf 'argv[%s]=%s\n' "$index" "$arg"
    index=$((index + 1))
  done
} > "${HARNESS_RECORD:?HARNESS_RECORD required}"
cat "$HARNESS_RECORD"
cat >/dev/null
EOF

  cat > "$STUB_DIR/codex" <<'EOF'
#!/usr/bin/env bash
{
  printf 'pwd=%s\n' "$(pwd -P)"
  printf 'CODEX_API_KEY=%s\n' "${CODEX_API_KEY:-unset}"
  printf 'OPENAI_API_KEY=%s\n' "${OPENAI_API_KEY:-unset}"
  printf 'OPENROUTER_API_KEY=%s\n' "${OPENROUTER_API_KEY:-unset}"
  printf 'ANTHROPIC_API_KEY=%s\n' "${ANTHROPIC_API_KEY:-unset}"
  printf 'ANTHROPIC_AUTH_TOKEN=%s\n' "${ANTHROPIC_AUTH_TOKEN:-unset}"
  index=0
  for arg in "$@"; do
    printf 'argv[%s]=%s\n' "$index" "$arg"
    index=$((index + 1))
  done
} > "${HARNESS_RECORD:?HARNESS_RECORD required}"
cat "$HARNESS_RECORD"
cat >/dev/null
EOF

  chmod +x "$STUB_DIR/claude" "$STUB_DIR/codex"
}

@test "claude host runs from --cwd directory" {
  run bash "$HOST_EXECUTOR" --prompt-file "$PROMPT_FILE" --cwd "$TARGET_DIR" --host claude

  [ "$status" -eq 0 ]
  grep -Fx "pwd=$TARGET_PWD" "$HARNESS_RECORD"
  grep -Fx "argv[0]=-p" "$HARNESS_RECORD"
}

@test "codex host receives -C with --cwd and defaults to workspace-write" {
  run bash "$HOST_EXECUTOR" --prompt-file "$PROMPT_FILE" --cwd "$TARGET_DIR" --host codex

  [ "$status" -eq 0 ]
  grep -Fx "argv[0]=exec" "$HARNESS_RECORD"
  grep -Fx "argv[1]=-C" "$HARNESS_RECORD"
  grep -Fx "argv[2]=$TARGET_PWD" "$HARNESS_RECORD"
  # codex-gate spec-205-gate-hardening (FINDING 4): least-privilege sandbox by default.
  grep -Fx "argv[4]=workspace-write" "$HARNESS_RECORD"
}

@test "codex host uses danger-full-access only with --full-access opt-in" {
  run bash "$HOST_EXECUTOR" --prompt-file "$PROMPT_FILE" --cwd "$TARGET_DIR" --host codex --full-access

  [ "$status" -eq 0 ]
  grep -Fx "argv[4]=danger-full-access" "$HARNESS_RECORD"
}

@test "codex host strips provider keys before invoking the CLI" {
  CODEX_API_KEY=should_not_pass \
  OPENAI_API_KEY=should_not_pass \
  OPENROUTER_API_KEY=should_not_pass \
  ANTHROPIC_API_KEY=should_not_pass \
  ANTHROPIC_AUTH_TOKEN=should_not_pass \
    run bash "$HOST_EXECUTOR" --prompt-file "$PROMPT_FILE" --cwd "$TARGET_DIR" --host codex

  [ "$status" -eq 0 ]
  grep -Fx "CODEX_API_KEY=unset" "$HARNESS_RECORD"
  grep -Fx "OPENAI_API_KEY=unset" "$HARNESS_RECORD"
  grep -Fx "OPENROUTER_API_KEY=unset" "$HARNESS_RECORD"
  grep -Fx "ANTHROPIC_API_KEY=unset" "$HARNESS_RECORD"
  grep -Fx "ANTHROPIC_AUTH_TOKEN=unset" "$HARNESS_RECORD"
}

@test "HOST_EXECUTOR_FULL_ACCESS=1 env also opts into danger-full-access" {
  HOST_EXECUTOR_FULL_ACCESS=1 run bash "$HOST_EXECUTOR" --prompt-file "$PROMPT_FILE" --cwd "$TARGET_DIR" --host codex

  [ "$status" -eq 0 ]
  grep -Fx "argv[4]=danger-full-access" "$HARNESS_RECORD"
}

@test "--cwd that escapes the containment root is rejected" {
  ESCAPE_DIR="$(cd "$BATS_TEST_TMPDIR" && pwd -P)"
  # Constrain to the target subdir, then point --cwd at its parent (escape via resolution).
  CONTAINED_ROOT="$TARGET_PWD"
  HOST_EXECUTOR_ROOT="$CONTAINED_ROOT" run bash "$HOST_EXECUTOR" --prompt-file "$PROMPT_FILE" --cwd "$ESCAPE_DIR" --host codex

  [ "$status" -eq 2 ]
  [[ "$output" == *"escapes containment root"* ]]
}

@test "--cwd with .. traversal outside root is rejected" {
  CONTAINED_ROOT="$TARGET_PWD"
  HOST_EXECUTOR_ROOT="$CONTAINED_ROOT" run bash "$HOST_EXECUTOR" --prompt-file "$PROMPT_FILE" --cwd "$TARGET_DIR/.." --host codex

  [ "$status" -eq 2 ]
  [[ "$output" == *"escapes containment root"* ]]
}
