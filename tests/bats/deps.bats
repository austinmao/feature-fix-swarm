#!/usr/bin/env bats
# deps.sh contract: roster check exit codes, JSON shape, and the install
# guarantees (repo-scoped only, idempotent, confirmation unless --yes).

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/deps.sh"
  STUBS="$BATS_TEST_TMPDIR/stubs"
  mkdir -p "$STUBS"
}

# minimal PATH that keeps the probes runnable but hides everything else
make_stub_path() {
  local tool
  for tool in "$@"; do
    ln -sf "$(command -v "$tool")" "$STUBS/$tool"
  done
}

@test "check exits 0 in a fully-provisioned checkout" {
  run bash "$SCRIPT" check
  [ "$status" -eq 0 ]
  [[ "$output" != *"MISSING"*"(required)"* ]]
}

@test "check exits 1 and names a remedy when a required binary is hidden" {
  make_stub_path bash python3 git sed printf cat command
  run env PATH="$STUBS" bash "$SCRIPT" check
  [ "$status" -eq 1 ]
  [[ "$output" == *"MISSING"* ]]
  [[ "$output" == *"remedy:"* ]]
}

@test "check --json emits a parseable array with required/status keys" {
  run bash -c "bash '$SCRIPT' check --json | python3 -c '
import json, sys
rows = json.load(sys.stdin)
assert isinstance(rows, list) and len(rows) >= 20
assert all({\"name\", \"kind\", \"required\", \"status\", \"remedy\"} <= set(r) for r in rows)
print(\"json-shape-ok\")
'"
  [[ "$output" == *"json-shape-ok"* ]]
}

@test "install is idempotent when everything is already satisfied" {
  run bash "$SCRIPT" install --yes
  [ "$status" -eq 0 ]
  [[ "$output" == *"already installed"* || "$output" == *"installed @opengsd/gsd-core"* ]]
  run bash "$SCRIPT" install --yes
  [ "$status" -eq 0 ]
  [[ "$output" == *"already installed"* ]]
}

@test "install refuses an unknown flag with a typed line" {
  run bash "$SCRIPT" install --bogus
  [ "$status" -eq 1 ]
  [[ "$output" == *"DEPS: unknown install flag"* ]]
}

@test "bare invocation prints usage and exits 1" {
  run bash "$SCRIPT"
  [ "$status" -eq 1 ]
  [[ "$output" == *"usage: deps.sh"* ]]
}
