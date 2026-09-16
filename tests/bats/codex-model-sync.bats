#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/codex-model-sync.sh"
  CODEX_ROOT="$BATS_TEST_TMPDIR/.codex"
  mkdir -p "$CODEX_ROOT/agents"
}

write_agent() {
  local name="$1" model="$2"
  cat > "$CODEX_ROOT/agents/$name.toml" <<EOF
name = "$name"
model = "$model"
model_reasoning_effort = "low"
developer_instructions = '''instructions'''
EOF
}

write_agent_without_model() {
  local name="$1"
  cat > "$CODEX_ROOT/agents/$name.toml" <<EOF
name = "$name"
developer_instructions = '''instructions'''
EOF
}

@test "Claude aliases materialize to the researched Codex model ladder" {
  write_agent gsd-planner-fable fable
  write_agent gsd-reviewer-opus opus
  write_agent gsd-executor-sonnet sonnet
  write_agent gsd-scout-haiku haiku

  run bash "$SCRIPT" "$CODEX_ROOT"
  [ "$status" -eq 0 ]
  grep -F 'model = "gpt-5.6-sol"' "$CODEX_ROOT/agents/gsd-planner-fable.toml"
  # spec-004 AC-004 effort split: fable materializes at xhigh, opus stays high
  grep -F 'model_reasoning_effort = "xhigh"' "$CODEX_ROOT/agents/gsd-planner-fable.toml"
  grep -F 'model = "gpt-5.6-sol"' "$CODEX_ROOT/agents/gsd-reviewer-opus.toml"
  grep -F 'model = "gpt-5.6-terra"' "$CODEX_ROOT/agents/gsd-executor-sonnet.toml"
  grep -F 'model_reasoning_effort = "medium"' "$CODEX_ROOT/agents/gsd-executor-sonnet.toml"
  grep -F 'model = "gpt-5.6-luna"' "$CODEX_ROOT/agents/gsd-scout-haiku.toml"
  grep -F 'model_reasoning_effort = "low"' "$CODEX_ROOT/agents/gsd-scout-haiku.toml"
}

@test "full Claude IDs map and custom Codex IDs remain untouched" {
  write_agent gsd-full-sonnet claude-sonnet-5
  write_agent custom sonnet
  before="$(cat "$CODEX_ROOT/agents/custom.toml")"

  run bash "$SCRIPT" "$CODEX_ROOT"
  [ "$status" -eq 0 ]
  grep -F 'model = "gpt-5.6-terra"' "$CODEX_ROOT/agents/gsd-full-sonnet.toml"
  [ "$(cat "$CODEX_ROOT/agents/custom.toml")" = "$before" ]
}

@test "known full Codex IDs retain their model and receive canonical effort pins" {
  write_agent gsd-native-luna gpt-5.6-luna
  write_agent gsd-native-terra gpt-5.6-terra
  write_agent gsd-native-sol gpt-5.6-sol

  run bash "$SCRIPT" "$CODEX_ROOT"
  [ "$status" -eq 0 ]
  grep -F 'model = "gpt-5.6-luna"' "$CODEX_ROOT/agents/gsd-native-luna.toml"
  grep -F 'model_reasoning_effort = "low"' "$CODEX_ROOT/agents/gsd-native-luna.toml"
  grep -F 'model = "gpt-5.6-terra"' "$CODEX_ROOT/agents/gsd-native-terra.toml"
  grep -F 'model_reasoning_effort = "medium"' "$CODEX_ROOT/agents/gsd-native-terra.toml"
  grep -F 'model = "gpt-5.6-sol"' "$CODEX_ROOT/agents/gsd-native-sol.toml"
  grep -F 'model_reasoning_effort = "high"' "$CODEX_ROOT/agents/gsd-native-sol.toml"
}

@test "full Codex role overrides pin generated agents without weakening alias pins" {
  write_agent_without_model gsd-volume
  write_agent_without_model gsd-execution
  write_agent_without_model gsd-judgment
  write_agent_without_model gsd-frontier
  CONFIG="$BATS_TEST_TMPDIR/native-config.json"
  cat > "$CONFIG" <<'JSON'
{"model_overrides":{"gsd-volume":"gpt-5.6-luna","gsd-execution":"gpt-5.6-terra","gsd-judgment":"gpt-5.6-sol","gsd-frontier":"fable"}}
JSON

  GSD_MODEL_CONFIG="$CONFIG" run bash "$SCRIPT" "$CODEX_ROOT"
  [ "$status" -eq 0 ]
  grep -F 'model = "gpt-5.6-luna"' "$CODEX_ROOT/agents/gsd-volume.toml"
  grep -F 'model_reasoning_effort = "low"' "$CODEX_ROOT/agents/gsd-volume.toml"
  grep -F 'model = "gpt-5.6-terra"' "$CODEX_ROOT/agents/gsd-execution.toml"
  grep -F 'model_reasoning_effort = "medium"' "$CODEX_ROOT/agents/gsd-execution.toml"
  grep -F 'model = "gpt-5.6-sol"' "$CODEX_ROOT/agents/gsd-judgment.toml"
  grep -F 'model_reasoning_effort = "high"' "$CODEX_ROOT/agents/gsd-judgment.toml"
  grep -F 'model = "gpt-5.6-sol"' "$CODEX_ROOT/agents/gsd-frontier.toml"
  grep -F 'model_reasoning_effort = "xhigh"' "$CODEX_ROOT/agents/gsd-frontier.toml"
}

@test "unknown or suffixed Codex IDs remain untouched" {
  write_agent gsd-unknown gpt-5.6-sol-untrusted
  before="$(cat "$CODEX_ROOT/agents/gsd-unknown.toml")"

  run bash "$SCRIPT" "$CODEX_ROOT"
  [ "$status" -eq 0 ]
  [ "$(cat "$CODEX_ROOT/agents/gsd-unknown.toml")" = "$before" ]
}

@test "materialization is idempotent" {
  write_agent gsd-executor sonnet
  bash "$SCRIPT" "$CODEX_ROOT"
  first="$(cat "$CODEX_ROOT/agents/gsd-executor.toml")"
  bash "$SCRIPT" "$CODEX_ROOT"
  [ "$(cat "$CODEX_ROOT/agents/gsd-executor.toml")" = "$first" ]
}

@test "a standalone fable alias keeps its emitted Sol xhigh pin on repeat sync" {
  write_agent gsd-planner fable
  CONFIG="$BATS_TEST_TMPDIR/no-planner-override.json"
  printf '%s\n' '{"model_overrides":{}}' > "$CONFIG"
  GSD_MODEL_CONFIG="$CONFIG" bash "$SCRIPT" "$CODEX_ROOT"
  first="$(cat "$CODEX_ROOT/agents/gsd-planner.toml")"
  grep -F 'model = "gpt-5.6-sol"' "$CODEX_ROOT/agents/gsd-planner.toml"
  grep -F 'model_reasoning_effort = "xhigh"' "$CODEX_ROOT/agents/gsd-planner.toml"

  GSD_MODEL_CONFIG="$CONFIG" bash "$SCRIPT" "$CODEX_ROOT"
  [ "$(cat "$CODEX_ROOT/agents/gsd-planner.toml")" = "$first" ]
}

@test "clean global-style TOMLs get model pins inserted from FFS config" {
  write_agent_without_model gsd-planner
  write_agent_without_model gsd-executor
  CONFIG="$BATS_TEST_TMPDIR/config.json"
  cat > "$CONFIG" <<'JSON'
{"model_overrides":{"gsd-planner":"fable","gsd-executor":"sonnet"}}
JSON

  GSD_MODEL_CONFIG="$CONFIG" run bash "$SCRIPT" "$CODEX_ROOT"
  [ "$status" -eq 0 ]
  grep -F 'model = "gpt-5.6-sol"' "$CODEX_ROOT/agents/gsd-planner.toml"
  # spec-004 AC-004: fable override materializes at xhigh
  grep -F 'model_reasoning_effort = "xhigh"' "$CODEX_ROOT/agents/gsd-planner.toml"
  grep -F 'model = "gpt-5.6-terra"' "$CODEX_ROOT/agents/gsd-executor.toml"
  grep -F 'model_reasoning_effort = "medium"' "$CODEX_ROOT/agents/gsd-executor.toml"
}
