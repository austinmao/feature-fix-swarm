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
  grep -F 'never launch a replacement while that pid is alive' "$BATS_TEST_TMPDIR/codex.args"
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}

@test "runner publishes a live pidfile and refuses a duplicate stateful drive" {
  cat > "$STUB_DIR/slow-codex" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/slow-drive-started"
sleep 2
echo SLOW_CODEX_OK
EOF
  chmod +x "$STUB_DIR/slow-codex"
  RUN_STATE="$BATS_TEST_TMPDIR/run-state"

  run env FFS_HOST=codex CODEX_BIN=slow-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" bash -c '
      bash "$1" /gsd-quick first >"$2/first.log" 2>&1 &
      first=$!
      i=0
      while [ ! -s "$3/gsd-run.pid" ] && [ "$i" -lt 100 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ -s "$3/gsd-run.pid" ] || exit 10
      live_pid=$(head -1 "$3/gsd-run.pid" | tr -d "[:space:]")
      kill -0 "$live_pid" || exit 11
      grep -E "^machine=.+" "$3/gsd-run.pid" || exit 19
      [ ! -e "$3/gsd-run.lock" ] || exit 20

      bash "$1" /gsd-quick duplicate >"$2/duplicate.log" 2>&1
      duplicate_rc=$?
      [ "$duplicate_rc" -eq 75 ] || exit 12
      grep -F "active drive already owns" "$2/duplicate.log" || exit 13
      [ "$(head -1 "$3/gsd-run.pid" | tr -d "[:space:]")" = "$live_pid" ] || exit 14

      wait "$first" || exit 15
      [ ! -e "$3/gsd-run.pid" ] || exit 16
      grep -F "state=completed" "$3/gsd-run.status" || exit 17
      grep -F "exit_code=0" "$3/gsd-run.status" || exit 18
    ' _ "$SCRIPT" "$BATS_TEST_TMPDIR" "$RUN_STATE"

  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/slow-drive-started" ]
}

@test "runner refuses a symlinked run-state directory before probing a host" {
  mkdir -p "$BATS_TEST_TMPDIR/real-state"
  ln -s "$BATS_TEST_TMPDIR/real-state" "$BATS_TEST_TMPDIR/symlink-state"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$BATS_TEST_TMPDIR/symlink-state" \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 75 ]
  [[ "$output" == *"refusing symlinked run-state directory"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
}

@test "live legacy one-line pidfile remains owned during an upgrade" {
  RUN_STATE="$BATS_TEST_TMPDIR/legacy-run-state"
  mkdir -p "$RUN_STATE"
  printf '%s\n' "$$" > "$RUN_STATE/gsd-run.pid"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_MACHINE_ID=local-host \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 75 ]
  [[ "$output" == *"active drive already owns"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "fresh foreign-machine ownership is never reclaimed as a dead local pid" {
  RUN_STATE="$BATS_TEST_TMPDIR/foreign-run-state"
  mkdir -p "$RUN_STATE"
  printf '%s\nmachine=%s\n' 2147483647 foreign-host > "$RUN_STATE/gsd-run.pid"
  touch "$RUN_STATE/gsd-run.heartbeat"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_MACHINE_ID=local-host \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 75 ]
  [[ "$output" == *"foreign owner"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
}

@test "fresh foreign claim outranks an old shared heartbeat during startup" {
  RUN_STATE="$BATS_TEST_TMPDIR/foreign-claim-run-state"
  mkdir -p "$RUN_STATE"
  touch "$RUN_STATE/gsd-run.heartbeat"
  touch -t 200001010000 "$RUN_STATE/gsd-run.heartbeat"
  printf '%s\nmachine=%s\nclaimed_epoch=%s\n' \
    2147483647 foreign-host "$(date +%s)" > "$RUN_STATE/gsd-run.pid"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_MACHINE_ID=local-host \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 75 ]
  [[ "$output" == *"foreign owner"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
}

@test "stale crashed reclaim mutex self-heals instead of stalling future drives" {
  RUN_STATE="$BATS_TEST_TMPDIR/stale-reclaim-run-state"
  mkdir -p "$RUN_STATE/gsd-run.reclaim"
  touch -t 200001010000 "$RUN_STATE/gsd-run.reclaim"

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_MACHINE_ID=local-host \
    run bash -c "cd '$BATS_TEST_TMPDIR' && bash '$SCRIPT' /gsd-quick test"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CODEX_OK"* ]]
  [ ! -e "$RUN_STATE/gsd-run.reclaim" ]
}

@test "heartbeat refresh atomically replaces a raced symlink without touching its target" {
  cat > "$STUB_DIR/heartbeat-codex" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *FFS_HOST_PROBE_READY* ]]; then
  echo FFS_HOST_PROBE_READY
  exit 0
fi
touch "$BATS_TEST_TMPDIR/heartbeat-drive-started"
sleep 3
echo HEARTBEAT_CODEX_OK
EOF
  chmod +x "$STUB_DIR/heartbeat-codex"
  RUN_STATE="$BATS_TEST_TMPDIR/heartbeat-run-state"
  TARGET="$BATS_TEST_TMPDIR/heartbeat-target"
  touch "$TARGET"
  touch -t 200001010000 "$TARGET"

  run env FFS_HOST=codex CODEX_BIN=heartbeat-codex CLAUDE_BIN=fake-claude \
    GSD_RUN_STATE_DIR="$RUN_STATE" GSD_HEARTBEAT_SECS=1 \
    bash -c '
      file_mtime() {
        stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
      }
      target_mtime="$(file_mtime "$4")"
      bash "$1" /gsd-quick heartbeat >"$2/heartbeat.log" 2>&1 &
      runner=$!
      i=0
      while [ ! -f "$2/heartbeat-drive-started" ] && [ "$i" -lt 100 ]; do
        sleep 0.02
        i=$((i + 1))
      done
      [ -f "$2/heartbeat-drive-started" ] || exit 30
      rm -f "$3/gsd-run.heartbeat"
      ln -s "$4" "$3/gsd-run.heartbeat"
      sleep 2
      [ ! -L "$3/gsd-run.heartbeat" ] || exit 31
      [ "$(file_mtime "$4")" = "$target_mtime" ] || exit 32
      wait "$runner"
    ' _ "$SCRIPT" "$BATS_TEST_TMPDIR" "$RUN_STATE" "$TARGET"

  [ "$status" -eq 0 ]
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

@test "requirement ownership mismatch blocks execute-phase before any host probe" {
  REPO="$BATS_TEST_TMPDIR/unsafe-plan"
  PHASE_DIR="$REPO/.planning/phases/02-example"
  mkdir -p "$PHASE_DIR"
  cat > "$REPO/.planning/ROADMAP.md" <<'EOF'
# Roadmap

## Phase 2: Example
**Requirements:** FR-001
EOF
  for plan in 01 02; do
    cat > "$PHASE_DIR/02-${plan}-PLAN.md" <<EOF
---
phase: 02-example
plan: "${plan}"
requirements: [FR-001]
---
EOF
  done

  FFS_HOST=codex CODEX_BIN=fake-codex CLAUDE_BIN=fake-claude \
    run bash -c "cd '$REPO' && bash '$SCRIPT' /gsd-execute-phase 2"

  [ "$status" -eq 2 ]
  [[ "$output" == *"FR-001 is owned by multiple plans"* ]]
  [ ! -f "$BATS_TEST_TMPDIR/codex.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.probed" ]
  [ ! -f "$BATS_TEST_TMPDIR/codex.args" ]
  [ ! -f "$BATS_TEST_TMPDIR/claude.args" ]
}
