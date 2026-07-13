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

