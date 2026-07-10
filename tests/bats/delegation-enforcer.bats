#!/usr/bin/env bats
# delegation-enforcer.sh — PreToolUse hook, prevention-first cost control.
# Auto-pins unpinned Agent/Task spawns from .planning/config.json
# model_overrides[subagent_type]. Advisory only — always exits 0.
# Covers: PATH-001 inject, byte-identical passthrough (pinned), no-config
# warn, EDGE-002 (config present, no model_overrides), EDGE-001 (non-Agent
# tool), AC-002 (DELEGATION_ENFORCER=off kill-switch), fail-open on
# malformed JSON.

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

@test "PATH-001: unpinned Agent JSON + seeded config -> injects tool_input.model" {
  seed_config
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","description":"do work"}}'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  echo "$output" | jq -e '.tool_input.model == "sonnet"'
}

@test "already-pinned Agent JSON -> byte-identical passthrough" {
  seed_config
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","model":"opus","description":"do work"}}'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ "$output" = "$INPUT" ]
}

@test "no .planning/config.json -> passthrough + non-empty stderr warn" {
  rm -f "$SANDBOX/.planning/config.json"
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","description":"do work"}}'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ "$output" = "$INPUT" ]

  run bash -c "bash '$HOOK' <<<'$INPUT' 2>&1 1>/dev/null"
  [ "$status" -eq 0 ]
  [ -n "$output" ]
}

@test "EDGE-002: config.json present but no model_overrides key -> passthrough + warn" {
  cat > "$SANDBOX/.planning/config.json" <<'EOF'
{
  "mode": "yolo"
}
EOF
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","description":"do work"}}'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ "$output" = "$INPUT" ]
}

@test "EDGE-001: non-Agent tool JSON (Bash) -> byte-identical passthrough" {
  seed_config
  INPUT='{"tool_name":"Bash","tool_input":{"command":"git status"}}'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ "$output" = "$INPUT" ]
}

@test "AC-002: DELEGATION_ENFORCER=off -> unconditional passthrough" {
  seed_config
  INPUT='{"tool_name":"Agent","tool_input":{"subagent_type":"gsd-executor","description":"do work"}}'
  run bash -c "DELEGATION_ENFORCER=off bash '$HOOK' <<<'$INPUT'"
  [ "$status" -eq 0 ]
  [ "$output" = "$INPUT" ]
}

@test "malformed JSON on stdin -> passthrough, never crash" {
  seed_config
  INPUT='not-json garbage {{{'
  run bash "$HOOK" <<<"$INPUT"
  [ "$status" -eq 0 ]
  [ "$output" = "$INPUT" ]
}
