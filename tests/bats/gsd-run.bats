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
if printf '%s\n' "\$@" | grep -q 'sandbox_mode="read-only"'; then
  case "\${FAKE_CODEX_MODE:-ok}" in
    usage) echo "You've hit your usage limit" >&2; exit 1 ;;
    bad_probe) echo "unexpected preflight response"; exit 0 ;;
    *) echo FFS-GSD-PREFLIGHT-OK; exit 0 ;;
  esac
fi
touch "$BATS_TEST_TMPDIR/codex-executed"
case "\${FAKE_CODEX_MODE:-ok}" in
  task) echo "executor task failed"; exit 42 ;;
  task_availability_text) echo "integration test failed: connection refused"; exit 42 ;;
  task_127) touch "$BATS_TEST_TMPDIR/codex-mutated-127"; echo "partial executor output"; exit 127 ;;
  timeout) touch "$BATS_TEST_TMPDIR/codex-mutated"; sleep 30 ;;
  *) echo CODEX_OK ;;
esac
EOF
  cat > "$STUB_DIR/fake-claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/claude.args"
if printf '%s\n' "\$@" | grep -q -- '--safe-mode'; then
  case "\${FAKE_CLAUDE_MODE:-ok}" in
    usage) echo "You've hit your session limit" >&2; exit 1 ;;
    *) echo FFS-GSD-PREFLIGHT-OK; exit 0 ;;
  esac
fi
touch "$BATS_TEST_TMPDIR/claude-executed"
case "\${FAKE_CLAUDE_MODE:-ok}" in
  task) echo "executor task failed"; exit 42 ;;
  *) echo CLAUDE_OK ;;
esac
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

@test "missing native Codex CLI falls back once to Claude" {
  FFS_HOST=codex CODEX_BIN=definitely-missing-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Codex CLI not found"* ]]
  [[ "$output" == *"falling back from codex to claude"* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "Codex usage limit falls back to Claude without waiting for reset" {
  FFS_HOST=codex FAKE_CODEX_MODE=usage CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"usage limit"* ]]
  [[ "$output" == *"falling back from codex to claude"* ]]
  [[ "$output" == *"CLAUDE_OK"* ]]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "Claude session limit falls back to Codex without waiting for reset" {
  FFS_HOST=claude FAKE_CLAUDE_MODE=usage CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"session limit"* ]]
  [[ "$output" == *"falling back from claude to codex"* ]]
  [[ "$output" == *"CODEX_OK"* ]]
}

@test "both failed model preflights launch no mutating drive" {
  FFS_HOST=codex FAKE_CODEX_MODE=usage FAKE_CLAUDE_MODE=usage \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 1 ]
  [[ "$output" == *"both bounded model preflights failed; no mutating drive launched"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex-executed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude-executed" ]
}

@test "fallback kill-switch fails after native preflight without crossing vendors" {
  FFS_HOST=codex FFS_CROSS_VENDOR_FALLBACK=off FAKE_CODEX_MODE=usage \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 1 ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex-executed" ]
}

@test "unacknowledged native preflight falls back before execution" {
  FFS_HOST=codex FAKE_CODEX_MODE=bad_probe CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"model preflight returned no valid acknowledgement"* ]]
  [[ "$output" == *"falling back from codex to claude before execution"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex-executed" ]
  [ -f "$BATS_TEST_TMPDIR/claude-executed" ]
}

@test "ordinary executor failure does not cross vendors and duplicate work" {
  FFS_HOST=codex FAKE_CODEX_MODE=task CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 42 ]
  [[ "$output" == *"executor task failed"* ]]
  [[ "$output" != *"falling back"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "task output containing availability prose does not manufacture fallback" {
  FFS_HOST=codex FAKE_CODEX_MODE=task_availability_text CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 42 ]
  [[ "$output" == *"connection refused"* ]]
  [[ "$output" != *"falling back"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "mutating exit 127 after partial execution is never replayed" {
  FFS_HOST=codex FAKE_CODEX_MODE=task_127 CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run -127 bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 127 ]
  [ -f "$BATS_TEST_TMPDIR/codex-mutated-127" ]
  [[ "$output" == *"partial executor output"* ]]
  [[ "$output" != *"falling back"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "mutating timeout fails promptly without ambiguous cross-vendor replay" {
  FFS_HOST=codex TIMEOUT=1 FAKE_CODEX_MODE=timeout CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ -f "$BATS_TEST_TMPDIR/codex-mutated" ]
  [[ "$output" == *"cross-vendor replay suppressed because execution state is ambiguous"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "project Codex agents without GSD roles do not shadow the global GSD config" {
  unset GSD_CODEX_CONFIG_ROOT
  REPO="$BATS_TEST_TMPDIR/repo"
  HOME_DIR="$BATS_TEST_TMPDIR/home"
  mkdir -p "$REPO/.codex/agents" "$REPO/.planning" "$HOME_DIR/.codex/agents"
  printf 'name = "unrelated"\n' > "$REPO/.codex/agents/unrelated.toml"
  cat > "$HOME_DIR/.codex/agents/gsd-executor.toml" <<'EOF'
name = "gsd-executor"
developer_instructions = '''instructions'''
EOF
  printf '%s\n' '{"model_overrides":{"gsd-executor":"sonnet"}}' > "$REPO/.planning/config.json"

  HOME="$HOME_DIR" FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -F 'model = "gpt-5.6-terra"' "$HOME_DIR/.codex/agents/gsd-executor.toml"
  [ ! -f "$REPO/.codex/agents/gsd-executor.toml" ]
}

@test "project-local GSD roles select the project Codex config root" {
  unset GSD_CODEX_CONFIG_ROOT
  REPO="$BATS_TEST_TMPDIR/project-repo"
  HOME_DIR="$BATS_TEST_TMPDIR/project-home"
  mkdir -p "$REPO/.codex/agents" "$REPO/.planning" "$HOME_DIR/.codex/agents"
  cat > "$REPO/.codex/agents/gsd-executor.toml" <<'EOF'
name = "gsd-executor"
developer_instructions = '''project instructions'''
EOF
  cat > "$HOME_DIR/.codex/agents/gsd-executor.toml" <<'EOF'
name = "gsd-executor"
developer_instructions = '''global instructions'''
EOF
  printf '%s\n' '{"model_overrides":{"gsd-executor":"sonnet"}}' > "$REPO/.planning/config.json"

  HOME="$HOME_DIR" FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -F 'model = "gpt-5.6-terra"' "$REPO/.codex/agents/gsd-executor.toml"
  ! grep -F 'model = ' "$HOME_DIR/.codex/agents/gsd-executor.toml"
}
