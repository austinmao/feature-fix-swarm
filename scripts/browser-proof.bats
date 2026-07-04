#!/usr/bin/env bats
# Tests for scripts/browser-proof.sh — driver ladder + base-url resolution +
# web-touch detection + FAIL-not-skip semantics (v3.20.0 Stream B).
#
# The old qa-swarm behavior silently SKIPPED e2e when no dev server answered
# on :3000 — a web-touching phase could pass QA with zero browser
# verification. These tests pin the replacement contract.

SCRIPT="$BATS_TEST_DIRNAME/browser-proof.sh"

setup() {
  export TMPBIN="$BATS_TMPDIR/bin-$$"
  mkdir -p "$TMPBIN"
  export ORIG_PATH="$PATH"
  unset QA_BASE_URL QA_ALLOW_NO_SERVER BROWSER_PROOF_PROBE_PORTS
}

teardown() {
  export PATH="$ORIG_PATH"
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$TMPBIN"
}

start_stub_server() {
  local port="$1"
  python3 -m http.server "$port" --bind 127.0.0.1 >/dev/null 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 20); do
    curl -s -o /dev/null "http://127.0.0.1:$port" && return 0
    sleep 0.2
  done
  return 1
}

# --------------------------------------------------------- web-touch detect

@test "non-web diff: WEB-TOUCH no, exit 0, no server needed" {
  run bash "$SCRIPT" --diff "lib/gates.py scripts/foo.sh README.md"
  [ "$status" -eq 0 ]
  [[ "$output" == *"WEB-TOUCH:no"* ]]
}

@test "tsx diff counts as web-touch" {
  export QA_ALLOW_NO_SERVER=1
  run bash "$SCRIPT" --diff "web/src/components/Button.tsx"
  [[ "$output" == *"WEB-TOUCH:yes"* ]]
}

@test "api route diff counts as web-touch (auth/api breakage shows in browser)" {
  export QA_ALLOW_NO_SERVER=1
  run bash "$SCRIPT" --diff "web/src/app/api/login/route.ts"
  [[ "$output" == *"WEB-TOUCH:yes"* ]]
}

@test "css diff counts as web-touch" {
  export QA_ALLOW_NO_SERVER=1
  run bash "$SCRIPT" --diff "web/src/styles/site.css"
  [[ "$output" == *"WEB-TOUCH:yes"* ]]
}

# --------------------------------------------------- fail-not-skip semantics

@test "web-touching diff with no server FAILS (exit 1), never skips" {
  export BROWSER_PROOF_PROBE_PORTS="59872"   # nothing listens here
  run bash "$SCRIPT" --diff "web/src/app/page.tsx"
  [ "$status" -eq 1 ]
  [[ "$output" == *"NO-SERVER"* ]]
}

@test "QA_ALLOW_NO_SERVER=1 waives explicitly (exit 0 + WAIVED marker)" {
  export BROWSER_PROOF_PROBE_PORTS="59872"
  export QA_ALLOW_NO_SERVER=1
  run bash "$SCRIPT" --diff "web/src/app/page.tsx"
  [ "$status" -eq 0 ]
  [[ "$output" == *"WAIVED"* ]]
}

# ------------------------------------------------------- base-url resolution

@test "QA_BASE_URL wins when reachable" {
  start_stub_server 59873
  export QA_BASE_URL="http://127.0.0.1:59873"
  run bash "$SCRIPT" --diff "web/src/app/page.tsx"
  [ "$status" -eq 0 ]
  [[ "$output" == *"BASE-URL:http://127.0.0.1:59873"* ]]
}

@test "unreachable QA_BASE_URL fails rather than silently probing elsewhere" {
  export QA_BASE_URL="http://127.0.0.1:59874"
  export BROWSER_PROOF_PROBE_PORTS="59872"
  run bash "$SCRIPT" --diff "web/src/app/page.tsx"
  [ "$status" -eq 1 ]
  [[ "$output" == *"NO-SERVER"* ]]
}

@test "probe list finds a running server" {
  start_stub_server 59875
  export BROWSER_PROOF_PROBE_PORTS="59872 59875"
  run bash "$SCRIPT" --diff "web/src/app/page.tsx"
  [ "$status" -eq 0 ]
  [[ "$output" == *"BASE-URL:http://127.0.0.1:59875"* ]]
}

# ---------------------------------------------------------- driver ladder

@test "canary CLI on PATH selects canary driver" {
  printf '#!/bin/sh\nexit 0\n' > "$TMPBIN/canary"; chmod +x "$TMPBIN/canary"
  export PATH="$TMPBIN:$PATH"
  start_stub_server 59876
  export QA_BASE_URL="http://127.0.0.1:59876"
  run bash "$SCRIPT" --diff "web/src/app/page.tsx"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRIVER:canary"* ]]
}

@test "playwright without canary selects playwright driver" {
  printf '#!/bin/sh\nexit 0\n' > "$TMPBIN/playwright"; chmod +x "$TMPBIN/playwright"
  export PATH="$TMPBIN:/usr/bin:/bin"   # canary NOT reachable
  start_stub_server 59877
  export QA_BASE_URL="http://127.0.0.1:59877"
  run bash "$SCRIPT" --diff "web/src/app/page.tsx"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRIVER:playwright"* ]]
}

@test "neither canary nor playwright falls back to agent driver" {
  export PATH="/usr/bin:/bin"
  start_stub_server 59878
  export QA_BASE_URL="http://127.0.0.1:59878"
  run bash "$SCRIPT" --diff "web/src/app/page.tsx"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRIVER:agent"* ]]
}
