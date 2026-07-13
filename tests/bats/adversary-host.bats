#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LIB="$ROOT/scripts/gsd/adversary-host.sh"
  unset FFS_HOST CODEX_SESSION_ID CODEX_THREAD_ID CODEX_AGENT CODEX_CI
  unset CLAUDE_SESSION_ID CLAUDE_CODE
}

@test "explicit FFS_HOST wins over ambient markers" {
  FFS_HOST=claude CODEX_THREAD_ID=thread-1 run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [ "$output" = "claude" ]
}

@test "CODEX_THREAD_ID detects a Codex session" {
  CODEX_THREAD_ID=thread-1 run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [ "$output" = "codex" ]
}

@test "CODEX_HOME alone is config, not proof of a Codex session" {
  CODEX_HOME=/tmp/codex-config FFS_HOST_PROCESS_DETECT=off \
    run bash -c ". '$LIB'; detect_orchestrator_host"
  [ "$status" -eq 0 ]
  [[ "$output" == *"defaulting to claude host"* ]]
  [[ "$output" == *"claude"* ]]
}

@test "opposite host mapping is symmetric" {
  run bash -c ". '$LIB'; adversary_kind_for_host claude"
  [ "$output" = "codex" ]
  run bash -c ". '$LIB'; adversary_kind_for_host codex"
  [ "$output" = "claude" ]
}

@test "availability classifier accepts exact provider diagnostics but rejects task prose" {
  diagnostics="$BATS_TEST_TMPDIR/provider.stderr"
  task_output="$BATS_TEST_TMPDIR/task.stdout"

  for message in \
    "You've hit your usage limit" \
    "Error: 429 rate limit exceeded" \
    "request failed: ECONNRESET" \
    "OAuth token has expired" \
    "stream disconnected before response"; do
    printf '%s\n' "$message" > "$diagnostics"
    run bash -c ". '$LIB'; vendor_failure_is_unavailable 1 '$diagnostics' mutating"
    [ "$status" -eq 0 ]
  done

  printf '%s\n' "integration test failed: connection refused" > "$task_output"
  run bash -c ". '$LIB'; vendor_failure_is_unavailable 42 '$task_output' mutating"
  [ "$status" -ne 0 ]
}

@test "large review prompt is streamed through stdin instead of one argv entry" {
  fake="$BATS_TEST_TMPDIR/fake-codex"
  args="$BATS_TEST_TMPDIR/codex.args"
  cat > "$fake" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$args"
bytes="\$(wc -c | tr -d '[:space:]')"
echo "BYTES: \$bytes"
echo "VERDICT: PASS"
EOF
  chmod +x "$fake"

  ADVERSARY_BIN_CODEX="$fake" run bash -c \
    ". '$LIB'; prompt=\$(python3 -c 'print(\"x\" * 1200000)'); adversary_invoke codex 30 gpt-5.6-sol xhigh \"\$prompt\""

  [ "$status" -eq 0 ]
  [[ "$output" == *"BYTES: 1200000"* ]]
  grep -Fx -- '-' "$args"
  [ "$(wc -c < "$args" | tr -d ' ')" -lt 1000 ]
}

@test "Claude review disables model tools, MCP, ordinary customizations, and persistence" {
  fake="$BATS_TEST_TMPDIR/fake-claude"
  args="$BATS_TEST_TMPDIR/claude.args"
  cat > "$fake" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$args"
cat >/dev/null
echo 'VERDICT: PASS'
EOF
  chmod +x "$fake"

  ADVERSARY_BIN_CLAUDE="$fake" run bash -c \
    ". '$LIB'; adversary_invoke claude 30 opus '' 'review data'"

  [ "$status" -eq 0 ]
  grep -Fx -- '--safe-mode' "$args"
  grep -Fx -- '--strict-mcp-config' "$args"
  grep -Fx -- '--tools' "$args"
  grep -Fx -- 'dontAsk' "$args"
  grep -Fx -- '--no-session-persistence' "$args"
  awk 'previous == "--tools" { found=(length($0) == 0); exit } { previous=$0 } END { exit (found ? 0 : 1) }' "$args"
}
