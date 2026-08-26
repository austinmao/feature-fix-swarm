#!/usr/bin/env bats

# Phase 1 takeover contracts.  These intentionally exercise the real
# collector/wall boundary with a private evidence store and local git only.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  COLLECTOR="$ROOT/skills/spec-status/scripts/collect-status-facts.sh"
  WALL="$ROOT/scripts/gsd/takeover-check.sh"
  GATES="$ROOT/lib/gates.py"
  REPO="$BATS_TEST_TMPDIR/repo"
  STORE="$BATS_TEST_TMPDIR/evidence.json"
  mkdir -p "$REPO/specs/006-autonomous-landing"
  cd "$REPO" || return 1
  git init -q
  git config user.email t@example.test
  git config user.name takeover-test
  touch README.md specs/006-autonomous-landing/spec.md
  git add README.md specs
  git commit -qm init
  git checkout -qb 006-takeover
}

@test "store-dir and store-path resolve without loading the ledger" {
  mkdir -p "$BATS_TEST_TMPDIR/store"
  run env GATES_STORE="$BATS_TEST_TMPDIR/store/evidence.json" python3 "$GATES" store-dir
  [ "$status" -eq 0 ]
  [ "$output" = "$BATS_TEST_TMPDIR/store" ]
  run env GATES_STORE="$BATS_TEST_TMPDIR/store/evidence.json" python3 "$GATES" store-path
  [ "$status" -eq 0 ]
  [ "$output" = "$BATS_TEST_TMPDIR/store/evidence.json" ]
}

@test "PATH-001 collector creates a record accepted by the wall" {
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  [ -f "$BATS_TEST_TMPDIR/takeover/spec-006.json" ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = "TAKEOVER-OK" ]
}

@test "absence split is explicit and expectation makes missing record refuse" {
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = "TAKEOVER-NONE" ]
  run env GATES_STORE="$STORE" python3 "$GATES" takeover-expect spec-006
  [ "$status" -eq 0 ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -ne 0 ]
  [[ "$output" == *"TAKEOVER-REFUSED:missing-record"* ]]
}

@test "writer refuses a planted output symlink without changing its target" {
  mkdir -p "$BATS_TEST_TMPDIR/takeover"
  printf sentinel > "$BATS_TEST_TMPDIR/target"
  ln -s "$BATS_TEST_TMPDIR/target" "$BATS_TEST_TMPDIR/takeover/spec-006.json"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -ne 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/target")" = sentinel ]
}

@test "wall JSON output shares the successful verdict" {
  mkdir -p "$BATS_TEST_TMPDIR/takeover"
  cat > "$BATS_TEST_TMPDIR/takeover/spec-006.json" <<EOF
{"schema_version":1,"created_at":1,"ids":{"spec_id":"006","run_id":"spec-006"},"gates_store":"$STORE","gates_store_anchor":"$(printf %s "$STORE" | shasum -a 256 | awk '{print $1}')","git_state":{"branch":"006-takeover","head":"$(git rev-parse HEAD)","upstream":""},"preflight":{},"grants":[],"pendings":[],"promotions":[],"runner":{},"unresolved_findings":[],"phases":[],"evidence":[],"forbid":[],"resume":{"command":"","preconditions":[]}}
EOF
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006 --json
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"TAKEOVER-OK"'* ]]
}
