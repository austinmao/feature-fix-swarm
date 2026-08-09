#!/usr/bin/env bats
# credential-output-guard.sh — PreToolUse blocker for commands that print
# credential values into an agent transcript. Mutations/injection through
# `doppler run` stay available; secret-name-only inventory stays available.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  HOOK="$REPO_ROOT/scripts/hooks/credential-output-guard.sh"
}

envelope() {
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$1"
}

@test "CG-001: doppler value output forms are blocked" {
  for command in \
    "doppler secrets --json" \
    "doppler secrets get API_TOKEN --plain" \
    "doppler secrets download --no-file --format json"; do
    run bash -c "$(declare -f envelope); envelope '$command' | bash '$HOOK'"
    [ "$status" -eq 2 ]
    [[ "$output" == *"credential values"* ]]
  done
}

@test "CG-002: railway variable and JSON config output forms are blocked" {
  for command in \
    "railway variables" \
    "railway variables --json" \
    "railway --service production variables --json" \
    "railway config --json"; do
    run bash -c "$(declare -f envelope); envelope '$command' | bash '$HOOK'"
    [ "$status" -eq 2 ]
  done
}

@test "CG-003: names-only inventory and env injection pass" {
  run bash -c "$(declare -f envelope); envelope 'doppler secrets --only-names' | bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope 'doppler run -p app -c prd -- ./deploy.sh' | bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "$(declare -f envelope); envelope 'railway whoami && railway status' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "CG-004: nested shell payload cannot bypass the guard" {
  for command in \
    "bash -lc 'railway variables --json'" \
    "bash -c -- 'doppler secrets --json'" \
    "bash -ec 'doppler secrets --json'" \
    "zsh -fc 'doppler secrets get API_TOKEN --plain'"; do
    run bash -c "$(declare -f envelope); envelope \"$command\" | bash '$HOOK'"
    [ "$status" -eq 2 ]
  done
}

@test "CG-005: Codex cmd envelopes are inspected" {
  python3 -c 'import json; print(json.dumps({"tool_name":"exec_command","tool_input":{"cmd":"doppler secrets --json"}}))' \
    > "$BATS_TEST_TMPDIR/envelope.json"
  run bash "$HOOK" < "$BATS_TEST_TMPDIR/envelope.json"
  [ "$status" -eq 2 ]
}

@test "CG-006: kill-switch and malformed input fail open" {
  run bash -c "$(declare -f envelope); envelope 'railway variables' | CREDENTIAL_OUTPUT_GUARD=off bash '$HOOK'"
  [ "$status" -eq 0 ]
  run bash -c "printf 'not-json' | bash '$HOOK'"
  [ "$status" -eq 0 ]
}

@test "CG-007: eval and common execution wrappers cannot bypass the guard" {
  for command in \
    "eval 'doppler secrets --json'" \
    "sudo doppler secrets --json" \
    "nice railway variables" \
    "nohup doppler secrets download --no-file --format json"; do
    run bash -c "$(declare -f envelope); envelope \"$command\" | bash '$HOOK'"
    [ "$status" -eq 2 ]
  done
}

@test "CG-008: command substitution cannot hide credential-value reads" {
  commands=(
    'echo `doppler secrets get API_TOKEN --plain`'
    'echo "$(doppler secrets --json)"'
    'printf "%s" "$(railway --service production variables --json)"'
  )
  for command in "${commands[@]}"; do
    run bash -c "$(declare -f envelope); envelope \"\$1\" | bash '$HOOK'" _ "$command"
    [ "$status" -eq 2 ]
  done
}

@test "CG-009: timeout and nested env wrappers cannot bypass the guard" {
  for command in \
    "timeout 10 doppler secrets --json" \
    "gtimeout 10 railway variables" \
    "sudo env SAFE=1 doppler secrets get API_TOKEN --plain" \
    "nice timeout 10 railway --service production variables --json"; do
    run bash -c "$(declare -f envelope); envelope \"$command\" | bash '$HOOK'"
    [ "$status" -eq 2 ]
  done
}

@test "CG-010: host pipe left open does not make the hook wait for EOF" {
  run python3 -c '
import json
import subprocess
import sys

hook = sys.argv[1]
payload = json.dumps({"tool_input": {"command": "git status"}}) + "\n"
proc = subprocess.Popen(
    ["bash", hook],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
proc.stdin.write(payload)
proc.stdin.flush()
try:
    rc = proc.wait(timeout=1)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait()
    raise SystemExit("hook waited for EOF instead of consuming the JSON line")
raise SystemExit(rc)
' "$HOOK"
  [ "$status" -eq 0 ]
}
