#!/usr/bin/env bats
# model-equivalents.sh — cross-vendor model equivalence map tests

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LIB="$ROOT/scripts/gsd/model-equivalents.sh"
  # shellcheck disable=SC1090
  source "$LIB"
}

@test "codex_equiv_model: fable alias -> gpt-5.6-sol" {
  run codex_equiv_model fable
  [ "$status" -eq 0 ]
  [ "$output" = "gpt-5.6-sol" ]
}

@test "codex_equiv_model: opus alias -> gpt-5.6-sol" {
  run codex_equiv_model opus
  [ "$status" -eq 0 ]
  [ "$output" = "gpt-5.6-sol" ]
}

@test "codex_equiv_model: sonnet alias -> gpt-5.6-terra" {
  run codex_equiv_model sonnet
  [ "$status" -eq 0 ]
  [ "$output" = "gpt-5.6-terra" ]
}

@test "codex_equiv_model: haiku alias -> gpt-5.6-luna" {
  run codex_equiv_model haiku
  [ "$status" -eq 0 ]
  [ "$output" = "gpt-5.6-luna" ]
}

@test "codex_equiv_effort: fable -> xhigh, opus -> high, sonnet -> medium, haiku -> low" {
  run codex_equiv_effort fable
  [ "$output" = "xhigh" ]
  run codex_equiv_effort opus
  [ "$output" = "high" ]
  run codex_equiv_effort sonnet
  [ "$output" = "medium" ]
  run codex_equiv_effort haiku
  [ "$output" = "low" ]
}

@test "codex_equiv_model: accepts full Claude model IDs" {
  run codex_equiv_model claude-fable-5
  [ "$output" = "gpt-5.6-sol" ]
  run codex_equiv_model claude-opus-5
  [ "$output" = "gpt-5.6-sol" ]
  run codex_equiv_model claude-sonnet-5
  [ "$output" = "gpt-5.6-terra" ]
  run codex_equiv_model claude-haiku-4-5-20251001
  [ "$output" = "gpt-5.6-luna" ]
}

@test "claude_equiv_model: reverse map collapses sol -> opus, never fable" {
  run claude_equiv_model gpt-5.6-sol
  [ "$status" -eq 0 ]
  [ "$output" = "opus" ]
  [ "$output" != "fable" ]
}

@test "claude_equiv_model: terra -> sonnet, luna -> haiku" {
  run claude_equiv_model gpt-5.6-terra
  [ "$output" = "sonnet" ]
  run claude_equiv_model gpt-5.6-luna
  [ "$output" = "haiku" ]
}

@test "unknown input is fail-soft: echoes input unchanged, returns 1" {
  run codex_equiv_model definitely-unknown-model
  [ "$status" -eq 1 ]
  [ "$output" = "definitely-unknown-model" ]

  run claude_equiv_model definitely-unknown-model
  [ "$status" -eq 1 ]
  [ "$output" = "definitely-unknown-model" ]

  run codex_equiv_effort definitely-unknown-model
  [ "$status" -eq 1 ]
  [ "$output" = "definitely-unknown-model" ]
}
