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

# --- cloud delegation gates (token membership; same tokenizer as above) ---
@test "deny: claude --cloud" {
  assert_denied 'claude --cloud "do the thing"'
}
@test "deny: claude --cloud=value" {
  assert_denied 'claude -p x --cloud=env_123'
}
@test "deny: rtk-prefixed claude --cloud" {
  assert_denied 'rtk claude --cloud x'
}
@test "deny: codex cloud exec" {
  assert_denied 'codex cloud exec --env E "task"'
}
@test "deny: codex cloud apply" {
  assert_denied 'codex cloud apply TASK_1'
}
@test "deny: codex cloud diff" {
  assert_denied 'codex cloud diff TASK_1'
}
@test "deny: cloud-dispatch.sh adopt" {
  assert_denied 'bash scripts/gsd/cloud-dispatch.sh adopt R1 T1'
}
@test "deny: cloud-dispatch.sh abandon (path form)" {
  assert_denied '/x/scripts/gsd/cloud-dispatch.sh abandon R1'
}
@test "deny: cloud_dispatch.py resolve" {
  assert_denied 'python3 lib/cloud_dispatch.py resolve R1 T1'
}
@test "deny: label names the verb" {
  run -2 bash "$HOOK" <<<"$(claude_json 'codex cloud apply T')"
  [[ "$output" == "operator-only-guard: codex cloud apply is operator-only;"* ]]
}
@test "deny: label claude --cloud" {
  run -2 bash "$HOOK" <<<"$(claude_json 'claude --cloud x')"
  [[ "$output" == "operator-only-guard: claude --cloud is operator-only;"* ]]
}
@test "deny: label cloud-dispatch verb" {
  run -2 bash "$HOOK" <<<"$(claude_json 'bash cloud-dispatch.sh abandon R')"
  [[ "$output" == "operator-only-guard: cloud-dispatch abandon is operator-only;"* ]]
}

@test "allow: claude --help" {
  assert_allowed 'claude --help'
}
@test "allow: codex cloud list" {
  assert_allowed 'codex cloud list'
}
@test "allow: codex cloud status X" {
  assert_allowed 'codex cloud status X'
}
@test "allow: cloud-dispatch.sh read-only verbs" {
  local v
  for v in status plan doctor start orchestrate env-spec; do
    assert_allowed "bash scripts/gsd/cloud-dispatch.sh $v R1"
  done
}
@test "allow: git status" {
  assert_allowed 'git status'
}
@test "allow: cloud words split across segments" {
  assert_allowed 'codex cloud list; echo apply'
}
@test "allow: cloud-dispatch.sh adopt in a different segment" {
  assert_allowed 'bash cloud-dispatch.sh status R; echo adopt'
}
@test "allow: codex exec without cloud" {
  assert_allowed 'codex exec "task"'
}
@test "allow: quoted echo of the cloud words is not an executable (position match)" {
  assert_allowed 'echo "claude --cloud"'
  assert_allowed 'echo "codex cloud apply"'
}
@test "allow: git grep -e codex -e cloud -e diff (the words as arguments)" {
  assert_allowed 'git grep -e codex -e cloud -e diff'
  assert_allowed 'grep -rn "claude --cloud" docs'
  assert_allowed 'rg codex cloud exec skills'
  assert_allowed 'man codex cloud exec'
}
@test "deny: absolute-path executables are matched by basename" {
  assert_denied '/opt/homebrew/bin/claude --cloud "x"'
  assert_denied '/usr/local/bin/codex cloud exec --env E "x"'
  assert_denied './bin/codex cloud apply T1'
}
@test "deny: leading env assignments and wrappers are stripped" {
  assert_denied 'FFS_X=1 claude --cloud x'
  assert_denied 'env -i FOO=1 /opt/bin/claude --cloud x'
  assert_denied 'command claude --cloud x'
  assert_denied 'exec codex cloud diff T1'
  assert_denied 'sudo -n codex cloud apply T1'
  assert_denied 'nohup claude --cloud x &'
  assert_denied 'time codex cloud exec x'
  assert_denied 'rtk proxy claude --cloud x'
}
@test "deny: sh -c and bash -c bodies" {
  assert_denied 'bash -c "claude --cloud x"'
  assert_denied "sh -lc 'codex cloud apply T1'"
  assert_denied 'bash -c "cd /x && /opt/homebrew/bin/claude --cloud y"'
}
@test "deny: python3 and bash launchers for the dispatcher, by basename" {
  assert_denied 'python3 /a/b/lib/cloud_dispatch.py abandon R1 T1'
  assert_denied 'python3 -u lib/cloud_dispatch.py adopt R1 T1 --oid x'
  assert_denied '/usr/bin/env bash /a/scripts/gsd/cloud-dispatch.sh resolve R1 T1'
}
@test "allow: claude with cloud as an ordinary value, codex exec cloud, other python scripts" {
  assert_allowed 'claude -p "cloud" --model x'
  assert_allowed 'codex exec cloud apply'
  assert_allowed 'python3 lib/other.py --cloud'
}
@test "allow: malformed cloud input fails open (not json)" {
  run -0 bash "$HOOK" <<<'claude --cloud'
  [ -z "$output" ]
}
