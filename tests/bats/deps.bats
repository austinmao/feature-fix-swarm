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
  # plus its exact-pin overlay in node_modules, and a fake HOME carrying the
  # staged external skills. Nothing here relies on the caller's checkout or
  # globally installed GSD bytes.
  local repo="$BATS_TEST_TMPDIR/repo" home="$BATS_TEST_TMPDIR/home" tool
  local target="$repo/node_modules/@opengsd/gsd-core/gsd-core/bin/lib/tdd-red-evidence.cjs"
  local executor="$repo/node_modules/@opengsd/gsd-core/agents/gsd-executor.md"
  local resume="$repo/node_modules/@opengsd/gsd-core/gsd-core/workflows/execute-phase.md"
  mkdir -p "$(dirname "$target")" "$(dirname "$executor")" "$(dirname "$resume")" "$repo/scripts/gsd" "$repo/patches" "$home"
  git -C "$repo" init -q
  cp "$ROOT/scripts/gsd/apply-gsd-core-overlay.py" "$repo/scripts/gsd/"
  cp "$ROOT/patches/gsd-core-overlay.json" "$repo/patches/"
  cp "$ROOT/node_modules/@opengsd/gsd-core/gsd-core/bin/lib/tdd-red-evidence.cjs" "$target"
  cp "$ROOT/node_modules/@opengsd/gsd-core/agents/gsd-executor.md" "$executor"
  cp "$ROOT/node_modules/@opengsd/gsd-core/gsd-core/workflows/execute-phase.md" "$resume"
  printf '{"name":"@opengsd/gsd-core","version":"1.13.0"}\n' > "$repo/node_modules/@opengsd/gsd-core/package.json"
  for skill in prompt-master socratic; do
    mkdir -p "$home/.agents/skills/$skill"
    touch "$home/.agents/skills/$skill/SKILL.md"
  done
  for tool in claude gh jq shasum ps; do
    printf '#!/bin/sh\nexit 0\n' > "$STUBS/$tool"
    chmod +x "$STUBS/$tool"
  done
  printf '#!/bin/sh\nprintf "v24.0.0\\n"\n' > "$STUBS/node"
  printf '#!/bin/sh\nprintf "10.0.0\\n"\n' > "$STUBS/npm"
  chmod +x "$STUBS/node" "$STUBS/npm"
  # python3 and git must be real (the script itself uses them); carry the
  # real filelock location past the fake HOME (pip --user installs are
  # HOME-relative)
  local pypath
  pypath="$(python3 -c 'import filelock, pathlib; print(pathlib.Path(filelock.__file__).parents[1])')"
  run bash -c "cd '$repo' && PATH='$STUBS:$PATH' HOME='$home' PYTHONPATH='$pypath' bash '$SCRIPT' check"
  [ "$status" -eq 0 ]
  [[ "$output" != *"(required)"* ]]
}

@test "check rejects Node below 24 and reports the Node 24 remedy" {
  printf '#!/bin/sh\nprintf "v23.9.0\\n"\n' > "$STUBS/node"
  chmod +x "$STUBS/node"

  run env PATH="$STUBS:$PATH" bash "$SCRIPT" check

  [ "$status" -eq 1 ]
  [[ "$output" == *"MISSING  node (required)"* ]]
  [[ "$output" == *"install or activate Node.js 24+"* ]]
}

@test "check rejects npm below 10 and reports the npm 10 remedy" {
  printf '#!/bin/sh\nprintf "9.9.0\\n"\n' > "$STUBS/npm"
  chmod +x "$STUBS/npm"

  run env PATH="$STUBS:$PATH" bash "$SCRIPT" check

  [ "$status" -eq 1 ]
  [[ "$output" == *"MISSING  npm (required)"* ]]
  [[ "$output" == *"install or activate npm 10+"* ]]
}

@test "check accepts minimum versions after wrapper notice lines" {
  printf '#!/bin/sh\nprintf "node wrapper notice x.y\\nv24.0.0\\n"\n' > "$STUBS/node"
  printf '#!/bin/sh\nprintf "npm notice x.y\\n10.0.0\\n"\n' > "$STUBS/npm"
  chmod +x "$STUBS/node" "$STUBS/npm"

  run env PATH="$STUBS:$PATH" bash "$SCRIPT" check

  [[ "$output" == *"ok       node"* ]]
  [[ "$output" == *"ok       npm"* ]]
}

@test "install refuses Node below 24 before changing repo dependencies" {
  printf '#!/bin/sh\nprintf "v23.9.0\\n"\n' > "$STUBS/node"
  chmod +x "$STUBS/node"

  run env PATH="$STUBS:$PATH" bash "$SCRIPT" install --yes

  [ "$status" -eq 1 ]
  [[ "$output" == *"DEPS: Node.js 24+ is required"* ]]
}

@test "install refuses npm below 10 before changing repo dependencies" {
  printf '#!/bin/sh\nprintf "v24.0.0\\n"\n' > "$STUBS/node"
  printf '#!/bin/sh\nprintf "9.9.0\\n"\n' > "$STUBS/npm"
  chmod +x "$STUBS/node" "$STUBS/npm"

  run env PATH="$STUBS:$PATH" bash "$SCRIPT" install --yes

  [ "$status" -eq 1 ]
  [[ "$output" == *"DEPS: npm 10+ is required"* ]]
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
