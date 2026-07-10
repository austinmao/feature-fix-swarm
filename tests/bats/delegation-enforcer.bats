#!/usr/bin/env bats
# delegation-enforcer.sh — PreToolUse hook, prevention-first cost control.
# Auto-pins unpinned Agent/Task spawns from .planning/config.json
# model_overrides[subagent_type]. Advisory only — always exits 0.
# Covers: PATH-001 inject, byte-identical passthrough (pinned), no-config
# warn, EDGE-002 (config present, no model_overrides), EDGE-001 (non-Agent
# tool), AC-002 (DELEGATION_ENFORCER=off kill-switch), fail-open on
# malformed JSON.

bats_require_minimum_version 1.5.0

HOOK="$BATS_TEST_DIRNAME/../../scripts/hooks/delegation-enforcer.sh"

setup() {
  SANDBOX="$BATS_TEST_TMPDIR/sandbox"
  mkdir -p "$SANDBOX/.planning"
  cd "$SANDBOX" || exit 1
}

seed_config() {
  cat > "$SANDBOX/.planning/config.json" <<'EOF'
{
  "model_overrides": {
    "gsd-executor": "sonnet"
  }
}
EOF
}

@test "PATH-001: unpinned Agent JSON + seeded config -> emits updatedInput hook response" {
  seed_config
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","description":"do work"}}'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.hookSpecificOutput.hookEventName == "PreToolUse"'
  echo "$output" | jq -e '.hookSpecificOutput.updatedInput.model == "sonnet"'
  echo "$output" | jq -e '.hookSpecificOutput.updatedInput.subagent_type == "gsd-executor"'
}

@test "already-pinned Agent JSON -> empty passthrough (no change)" {
  seed_config
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","model":"opus","description":"do work"}}'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "no .planning/config.json -> empty passthrough + non-empty stderr warn" {
  rm -f "$SANDBOX/.planning/config.json"
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","description":"do work"}}'
  run --separate-stderr bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  [ -n "$stderr" ]
}

@test "EDGE-002: config.json present but no model_overrides key -> empty passthrough + warn" {
  cat > "$SANDBOX/.planning/config.json" <<'EOF'
{
  "mode": "yolo"
}
EOF
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","description":"do work"}}'
  run --separate-stderr bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  [ -n "$stderr" ]
}

@test "EDGE-001: non-Agent tool JSON (Bash) -> empty passthrough" {
  seed_config
  INPUT='{"tool_name":"Bash","tool_input":{"command":"git status"}}'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "AC-002: DELEGATION_ENFORCER=off -> unconditional empty passthrough" {
  seed_config
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","description":"do work"}}'
  run bash -c "DELEGATION_ENFORCER=off bash '$HOOK' <<<'$INPUT'"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "malformed JSON on stdin -> empty passthrough, never crash" {
  seed_config
  INPUT='not-json garbage {{{'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}
