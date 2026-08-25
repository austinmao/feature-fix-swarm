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

@test "check exits 0 in a fully-provisioned environment (hermetic)" {
  # Build a provisioned world from scratch so this passes on any runner:
  # stubs for every required binary, a scratch repo with the pinned gsd-core
  # in node_modules, and a fake HOME carrying the staged external skills.
  local repo="$BATS_TEST_TMPDIR/repo" home="$BATS_TEST_TMPDIR/home" tool
  mkdir -p "$repo/node_modules/@opengsd/gsd-core" "$home"
  git -C "$repo" init -q
  printf '{"version": "1.11.0"}\n' > "$repo/node_modules/@opengsd/gsd-core/package.json"
  for skill in prompt-master socratic; do
    mkdir -p "$home/.agents/skills/$skill"
    touch "$home/.agents/skills/$skill/SKILL.md"
  done
  for tool in node npm claude gh jq shasum ps; do
    printf '#!/bin/sh\nexit 0\n' > "$STUBS/$tool"
    chmod +x "$STUBS/$tool"
  done
  # python3 and git must be real (the script itself uses them); carry the
  # real filelock location past the fake HOME (pip --user installs are
  # HOME-relative)
  local pypath
  pypath="$(python3 -c 'import filelock, pathlib; print(pathlib.Path(filelock.__file__).parents[1])')"
  run bash -c "cd '$repo' && PATH='$STUBS:$PATH' HOME='$home' PYTHONPATH='$pypath' bash '$SCRIPT' check"
  [ "$status" -eq 0 ]
  [[ "$output" != *"(required)"* ]]
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
