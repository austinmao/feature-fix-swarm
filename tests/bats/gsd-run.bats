#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/gsd-run.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  CODEX_SOURCE_ROOT="$BATS_TEST_TMPDIR/codex-root"
  CLAUDE_SKILLS_ROOT="$BATS_TEST_TMPDIR/claude-skills"
  mkdir -p "$STUB_DIR" \
    "$CODEX_SOURCE_ROOT/skills/gsd-quick" "$CODEX_SOURCE_ROOT/agents" \
    "$CLAUDE_SKILLS_ROOT/gsd-quick"
  printf '%s\n' '---' 'name: gsd-quick' '---' > "$CODEX_SOURCE_ROOT/skills/gsd-quick/SKILL.md"
  printf '%s\n' 'name = "gsd-executor"' 'model = "sonnet"' > "$CODEX_SOURCE_ROOT/agents/gsd-executor.toml"
  printf '%s\n' '---' 'name: gsd-quick' '---' > "$CLAUDE_SKILLS_ROOT/gsd-quick/SKILL.md"

  cat > "$STUB_DIR/fake-codex" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  touch "$BATS_TEST_TMPDIR/codex.probed"
  case "\${FAKE_CODEX_PROBE_MODE:-ok}" in
    bad_ack) echo 'probe responded without acknowledgement'; exit 0 ;;
    fail) echo 'native quota exhausted API_TOKEN=super-secret-value-123456789' >&2; exit 69 ;;
    sol_unavailable)
      if [[ "\$*" == *gpt-5.6-sol* ]]; then
        echo 'requested Codex model unavailable' >&2
        exit 69
      fi
      ;;
  esac
  echo FFS_HOST_PROBE_READY
  exit 0
fi
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/codex.args"
printf '%s\n' "\${CODEX_HOME:-}" > "$BATS_TEST_TMPDIR/codex.home"
printf '%s\n' "\${CODEX_HOME:-}"/skills/*/SKILL.md > "$BATS_TEST_TMPDIR/codex.skills"
cat "\${CODEX_HOME:-}"/skills/*/SKILL.md > "$BATS_TEST_TMPDIR/codex.skill-content"
echo CODEX_OK
EOF
  cat > "$STUB_DIR/fake-claude" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  touch "$BATS_TEST_TMPDIR/claude.probed"
  case "\${FAKE_CLAUDE_PROBE_MODE:-ok}" in
    bad_ack) echo 'probe responded without acknowledgement'; exit 0 ;;
    fail) echo 'alternate model unavailable' >&2; exit 69 ;;
  esac
  echo FFS_HOST_PROBE_READY
  exit 0
fi
printf '%s\n' "\$@" > "$BATS_TEST_TMPDIR/claude.args"
echo CLAUDE_OK
EOF
  chmod +x "$STUB_DIR/fake-codex" "$STUB_DIR/fake-claude"
  export PATH="$STUB_DIR:$PATH"
  export GSD_CODEX_CONFIG_ROOT="$CODEX_SOURCE_ROOT"
  export GSD_CLAUDE_SKILLS_ROOT="$CLAUDE_SKILLS_ROOT"
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
  grep -F 'poll that exact session with write_stdin until it exits' "$BATS_TEST_TMPDIR/codex.args"
  grep -F '/skills/gsd-quick/SKILL.md' "$BATS_TEST_TMPDIR/codex.skills"
  [ "$(wc -l < "$BATS_TEST_TMPDIR/codex.skills" | tr -d ' ')" -eq 1 ]
}

@test "Codex GSD probe falls from unavailable Sol to Terra before crossing vendors" {
  FFS_HOST=codex GSD_LEAD_MODEL=opus FAKE_CODEX_PROBE_MODE=sol_unavailable \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'stay native'"

  [ "$status" -eq 0 ]
  [[ "$output" == *"selected gpt-5.6-terra"* ]]
  [ -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  grep -F 'model="gpt-5.6-terra"' "$BATS_TEST_TMPDIR/codex.args"
}

@test "Codex execution prompt forbids retrying a still-live yielded session" {
  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick 'run a long gate'"

  [ "$status" -eq 0 ]
  grep -F 'Script running with cell ID' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'session_id and no exit_code' "$BATS_TEST_TMPDIR/codex.args"
  grep -F 'Never launch a replacement command while the original session is alive' "$BATS_TEST_TMPDIR/codex.args"
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
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

@test "missing native Codex CLI is detected before launch and selects Claude" {
  FFS_HOST=codex CODEX_BIN=definitely-missing-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"native Codex unavailable before launch"* ]]
  [[ "$output" == *"CLAUDE_OK"* ]]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "native task failure mentioning API error never replays on alternate host" {
  cat > "$STUB_DIR/native-fails" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/native-started"
echo 'API error while executing the task'
exit 42
EOF
  cat > "$STUB_DIR/alternate-must-not-run" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/alternate-ran"
echo FFS_HOST_PROBE_READY
EOF
  chmod +x "$STUB_DIR/native-fails" "$STUB_DIR/alternate-must-not-run"

  FFS_HOST=codex CODEX_BIN=native-fails CLAUDE_BIN=alternate-must-not-run \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 42 ]
  [ -f "$BATS_TEST_TMPDIR/native-started" ]
  [ ! -f "$BATS_TEST_TMPDIR/alternate-ran" ]
  [[ "$output" == *"API error"* ]]
  [[ "$output" == *"resume on codex"* ]]
}

@test "timeout after native drive starts never replays on alternate host" {
  cat > "$STUB_DIR/native-times-out" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/native-started"
sleep 30
EOF
  cat > "$STUB_DIR/alternate-must-not-run" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/alternate-ran"
echo FFS_HOST_PROBE_READY
EOF
  chmod +x "$STUB_DIR/native-times-out" "$STUB_DIR/alternate-must-not-run"

  FFS_HOST=codex CODEX_BIN=native-times-out CLAUDE_BIN=alternate-must-not-run TIMEOUT=1 \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 124 ]
  [ -f "$BATS_TEST_TMPDIR/native-started" ]
  [ ! -f "$BATS_TEST_TMPDIR/alternate-ran" ]
  [[ "$output" == *"resume on codex"* ]]
}

@test "native preflight failure selects available alternate before the stateful drive" {
  cat > "$STUB_DIR/native-unavailable" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/native-probed"
exit 69
EOF
  cat > "$STUB_DIR/alternate-available" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  touch "$BATS_TEST_TMPDIR/alternate-probed"
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/alternate-drive"
echo ALTERNATE_OK
EOF
  chmod +x "$STUB_DIR/native-unavailable" "$STUB_DIR/alternate-available"

  FFS_HOST=codex CODEX_BIN=native-unavailable CLAUDE_BIN=alternate-available \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/native-probed" ]
  [ -f "$BATS_TEST_TMPDIR/alternate-probed" ]
  [ -f "$BATS_TEST_TMPDIR/alternate-drive" ]
  [[ "$output" == *"ALTERNATE_OK"* ]]
  [[ "$output" == *"selected Claude before launch"* ]]
}

@test "forensic opt-out stops after native probe failure without touching alternate" {
  FFS_HOST=codex FFS_CROSS_VENDOR_FALLBACK=off \
    FAKE_CODEX_PROBE_MODE=fail CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 69 ]
  [ -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  [[ "$output" == *"cross-vendor fallback disabled"* ]]
}

@test "bad native acknowledgement is diagnosed then alternate is selected before launch" {
  FFS_HOST=codex FAKE_CODEX_PROBE_MODE=bad_ack \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [[ "$output" == *"missing acknowledgement"* ]]
  [[ "$output" == *"selected Claude before launch"* ]]
}

@test "both failed probes emit redacted diagnostics and launch no stateful drive" {
  FFS_HOST=codex FAKE_CODEX_PROBE_MODE=fail FAKE_CLAUDE_PROBE_MODE=fail \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 69 ]
  [ -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
  [[ "$output" == *"native quota exhausted"* ]]
  [[ "$output" != *"super-secret-value"* ]]
  [[ "$output" == *"no usable host before launch"* ]]
  grep -Fq 'native quota exhausted' "$BATS_TEST_TMPDIR/.planning/logs/"*.log
  ! grep -Fq 'super-secret-value' "$BATS_TEST_TMPDIR/.planning/logs/"*.log
}

@test "operator TERM during a hanging native probe exits without probing or driving the alternate" {
  cat > "$STUB_DIR/native-probe-hangs" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/native-probe-started"
sleep 30
EOF
  cat > "$STUB_DIR/alternate-must-not-start" <<EOF
#!/usr/bin/env bash
touch "$BATS_TEST_TMPDIR/alternate-started"
echo FFS_HOST_PROBE_READY
EOF
  chmod +x "$STUB_DIR/native-probe-hangs" "$STUB_DIR/alternate-must-not-start"

  run env FFS_HOST=codex CODEX_BIN=native-probe-hangs \
    CLAUDE_BIN=alternate-must-not-start GSD_HOST_PROBE_TIMEOUT=2 \
    bash -c '
      bash "$1" /gsd-quick test >"$2/runner.log" 2>&1 &
      runner_pid=$!
      i=0
      while [ ! -f "$2/native-probe-started" ] && [ "$i" -lt 100 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      kill -TERM "$runner_pid"
      wait "$runner_pid"
    ' _ "$SCRIPT" "$BATS_TEST_TMPDIR"

  [ "$status" -eq 143 ]
  [ -f "$BATS_TEST_TMPDIR/native-probe-started" ]
  [ ! -f "$BATS_TEST_TMPDIR/alternate-started" ]
}

@test "model probe passes but missing exact Codex GSD skill selects Claude before launch" {
  MISSING_ROOT="$BATS_TEST_TMPDIR/codex-without-requested-skill"
  mkdir -p "$MISSING_ROOT/skills" "$MISSING_ROOT/agents" \
    "$CLAUDE_SKILLS_ROOT/gsd-plan-phase"
  printf '%s\n' '---' 'name: gsd-plan-phase' '---' \
    > "$CLAUDE_SKILLS_ROOT/gsd-plan-phase/SKILL.md"

  FFS_HOST=codex GSD_CODEX_CONFIG_ROOT="$MISSING_ROOT" \
    CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-plan-phase 2 --auto"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ -f "$BATS_TEST_TMPDIR/claude.args" ]
  [[ "$output" == *"exact gsd-plan-phase surface unavailable"* ]]
  [[ "$output" == *"selected Claude before launch"* ]]
}

@test "unrelated project Codex agents do not shadow the global exact GSD surface" {
  unset GSD_CODEX_CONFIG_ROOT
  REPO="$BATS_TEST_TMPDIR/unrelated-project"
  GLOBAL="$BATS_TEST_TMPDIR/global-codex"
  mkdir -p "$REPO/.codex/agents" "$GLOBAL/skills/gsd-quick" "$GLOBAL/agents"
  printf '%s\n' 'name = "unrelated"' > "$REPO/.codex/agents/unrelated.toml"
  printf '%s\n' '---' 'name: gsd-quick' 'marker: global-surface' '---' \
    > "$GLOBAL/skills/gsd-quick/SKILL.md"
  printf '%s\n' 'name = "gsd-executor"' > "$GLOBAL/agents/gsd-executor.toml"

  FFS_HOST=codex CODEX_HOME="$GLOBAL" CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -Fq 'marker: global-surface' "$BATS_TEST_TMPDIR/codex.skill-content"
}

@test "project-local exact GSD skill and roles select the project Codex surface" {
  unset GSD_CODEX_CONFIG_ROOT
  REPO="$BATS_TEST_TMPDIR/gsd-project"
  GLOBAL="$BATS_TEST_TMPDIR/global-codex"
  mkdir -p "$REPO/.codex/skills/gsd-quick" "$REPO/.codex/agents" \
    "$GLOBAL/skills/gsd-quick" "$GLOBAL/agents"
  printf '%s\n' '---' 'name: gsd-quick' 'marker: project-surface' '---' \
    > "$REPO/.codex/skills/gsd-quick/SKILL.md"
  printf '%s\n' 'name = "gsd-executor"' > "$REPO/.codex/agents/gsd-executor.toml"
  printf '%s\n' '---' 'name: gsd-quick' 'marker: global-surface' '---' \
    > "$GLOBAL/skills/gsd-quick/SKILL.md"
  printf '%s\n' 'name = "gsd-executor"' > "$GLOBAL/agents/gsd-executor.toml"

  FFS_HOST=codex CODEX_HOME="$GLOBAL" CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  grep -Fq 'marker: project-surface' "$BATS_TEST_TMPDIR/codex.skill-content"
  ! grep -Fq 'marker: global-surface' "$BATS_TEST_TMPDIR/codex.skill-content"
}
