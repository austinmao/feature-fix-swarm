#!/usr/bin/env bats
# operator-only-guard.sh — PreToolUse (Bash) anti-accident deny (exit 2) for
# operator-only gate commands (FFS #253). Fail-open on malformed input.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  HOOK="$ROOT/scripts/hooks/operator-only-guard.sh"
}

# claude_json <command> -> Claude shape; codex_json <command> -> Codex shape.
claude_json() { python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$1"; }
codex_json() { python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"cmd":sys.argv[1]}}))' "$1"; }

assert_denied() {
  run -2 bash "$HOOK" <<<"$(claude_json "$1")"
  [[ "$output" == "operator-only-guard: "*"is operator-only; the operator runs it in their own shell (FFS #253)" ]]
}
assert_allowed() {
  run -0 bash "$HOOK" <<<"$(claude_json "$1")"
  [ -z "$output" ]
}

@test "deny: findings-queue resolve --disposition issue" {
  assert_denied 'python3 lib/gates.py findings-queue resolve abc --disposition issue'
}
@test "deny: findings-queue resolve --disposition descope" {
  assert_denied 'python3 lib/gates.py findings-queue resolve abc --disposition descope'
}
@test "deny: findings-queue retag --in-spec via quoted variable path" {
  assert_denied 'python3 "$GATES_PY" findings-queue retag abc --in-spec'
}
@test "deny: intent-check --repin" {
  assert_denied 'python3 lib/gates.py intent-check --spec specs/123-x/spec.md --repin'
}
@test "deny: rtk-prefixed intent-check --repin" {
  assert_denied 'rtk python3 lib/gates.py intent-check --spec s.md --repin'
}
@test "deny: timeout wrapper with python flags" {
  assert_denied 'timeout 30 python3 -u lib/gates.py findings-queue resolve abc --disposition descope'
}
@test "deny: bash -lc quoted payload" {
  assert_denied "bash -lc 'python3 lib/gates.py findings-queue retag abc --in-spec'"
}
@test "deny: env assignment prefix" {
  assert_denied 'FOO=1 python3 lib/gates.py intent-check --repin --spec s.md'
}
@test "deny: after cd && chain" {
  assert_denied 'cd x && python3 lib/gates.py findings-queue resolve abc --disposition issue'
}
@test "deny: unbalanced quote" {
  assert_denied 'python3 lib/gates.py intent-check --repin --spec "s.md'
}
@test "deny: Codex cmd shape" {
  run -2 bash "$HOOK" <<<"$(codex_json 'python3 lib/gates.py intent-check --spec s.md --repin')"
  [[ "$output" == "operator-only-guard: "* ]]
}
@test "deny: list-valued cmd is joined" {
  run -2 bash "$HOOK" <<<'{"tool_name":"Bash","tool_input":{"cmd":["python3","lib/gates.py","findings-queue","retag","abc","--in-spec"]}}'
  [[ "$output" == "operator-only-guard: "* ]]
}

@test "deny: parenthesised quoted reason then --disposition issue" {
  assert_denied 'python3 lib/gates.py findings-queue resolve S --reason "tracked (filed #300)" --disposition issue'
}
@test "deny: semicolon inside quoted reason" {
  assert_denied 'python3 lib/gates.py findings-queue resolve S --reason "a; b" --disposition issue'
}
@test "deny: backslash-newline continuation (resolve)" {
  assert_denied $'python3 lib/gates.py findings-queue resolve S \\\n --disposition issue'
}
@test "deny: backslash-newline continuation (intent-check)" {
  assert_denied $'python3 lib/gates.py intent-check \\\n --spec x --repin'
}
@test "deny: backslash-newline continuation (retag)" {
  assert_denied $'python3 lib/gates.py findings-queue retag \\\n S --in-spec'
}
@test "deny: backtick substitution" {
  assert_denied 'echo `python3 lib/gates.py intent-check --spec s.md --repin`'
}
@test "deny: --in-spec glued to redirect" {
  assert_denied 'python3 lib/gates.py findings-queue retag S --in-spec>/dev/null'
}
@test "deny: --in-spec glued to here-string" {
  assert_denied 'python3 lib/gates.py findings-queue retag S --in-spec<<<y'
}

@test "allow: --disposition fix" {
  assert_allowed 'python3 lib/gates.py findings-queue resolve abc --disposition fix'
}
@test "allow: --disposition refute" {
  assert_allowed 'python3 lib/gates.py findings-queue resolve abc --disposition refute'
}
@test "allow: --disposition waive" {
  assert_allowed 'python3 lib/gates.py findings-queue resolve abc --disposition waive'
}
@test "allow: retag without --in-spec" {
  assert_allowed 'python3 lib/gates.py findings-queue retag abc'
}
@test "allow: intent-check --stage finish" {
  assert_allowed 'python3 lib/gates.py intent-check --spec s.md --stage finish'
}
@test "allow: words split across segments" {
  assert_allowed 'python3 lib/gates.py findings-queue resolve abc --disposition fix; echo issue'
}
@test "allow: empty stdin" {
  run -0 bash "$HOOK" <<<''
  [ -z "$output" ]
}
@test "allow: not json" {
  run -0 bash "$HOOK" <<<'not json'
  [ -z "$output" ]
}
@test "allow: JSON with no tool_input" {
  run -0 bash "$HOOK" <<<'{"tool_name":"Bash"}'
  [ -z "$output" ]
}

@test "pinned false positive: echo of the words is denied" {
  assert_denied 'echo "intent-check --repin"'
}
