#!/usr/bin/env bats
# cli-hang-guard.sh — PreToolUse (Bash) blocker for unbounded vendor-CLI calls.
# Block = exit 2 (hang-prone execution form, no visible bound); everything
# else passes (exit 0), including parse failures (fail-open).

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  HOOK="$REPO_ROOT/scripts/hooks/cli-hang-guard.sh"
}

envelope() {
  # envelope <command string> — synthetic PreToolUse Bash envelope
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$1"
}

@test "HG-001: bare codex exec -> BLOCKED (exit 2)" {
  run bash -c "envelope() { python3 -c 'import json,sys; print(json.dumps({\"tool_name\":\"Bash\",\"tool_input\":{\"command\":sys.argv[1]}}))' \"\$1\"; }; envelope 'codex exec \"review this plan\"' | bash '$HOOK'"
  [ "$status" -eq 2 ]
  [[ "$output" == *"BLOCKED"* ]]
  [[ "$output" == *"adversary-host"* ]]
}

@test "HG-002: timeout-wrapped codex exec -> passes" {
  run bash -c "$(declare -f envelope); envelope 'timeout 540 codex exec \"hi\" </dev/null' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "HG-003: version/status probes -> pass" {
  run bash -c "$(declare -f envelope); envelope 'codex --version && codex login status && claude --version' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "HG-004: bare claude -p -> BLOCKED (exit 2)" {
  run bash -c "$(declare -f envelope); envelope 'claude -p \"summarize\" --model claude-sonnet-5' | bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "HG-005: bare env-prefixed claude --print -> BLOCKED (exit 2)" {
  run bash -c "$(declare -f envelope); envelope 'env -u ANTHROPIC_API_KEY claude --print \"x\"' | bash '$HOOK'"
  [ "$status" -eq 2 ]
}

@test "HG-006: sanctioned lever invocations -> pass" {
  run bash -c "$(declare -f envelope); envelope 'TIMEOUT=3600 bash scripts/gsd/gsd-run.sh /gsd-plan-phase 2' | bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope 'bash scripts/gsd/model-fallback.sh .planning' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "HG-007: kill-switch CLI_HANG_GUARD=off -> passes a bare codex exec" {
  run bash -c "$(declare -f envelope); envelope 'codex exec \"hi\"' | CLI_HANG_GUARD=off bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "HG-008: malformed JSON / empty input -> fail-open (exit 0)" {
  run bash -c "printf 'not json' | bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "printf '' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "HG-009: unrelated Bash commands -> pass" {
  run bash -c "$(declare -f envelope); envelope 'git status && ls -la' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}
