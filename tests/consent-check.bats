#!/usr/bin/env bats

REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/gsd/consent-check.sh"

setup() {
  GSD_TOOLS_DIR="$BATS_TEST_TMPDIR/node_modules/.bin"
  mkdir -p "$GSD_TOOLS_DIR"

  cat > "$GSD_TOOLS_DIR/gsd-tools" <<'EOF'
console.log(JSON.stringify([
  { id: "test-cap-active", status: "active" },
  { id: "test-cap-inactive", status: "disabled" }
]));
EOF
}

@test "active capability id exits 0" {
  run env CONSENT_CHECK_REPO_ROOT="$BATS_TEST_TMPDIR" bash "$SCRIPT" test-cap-active

  [ "$status" -eq 0 ]
}

@test "absent capability id exits 1 with not active/consented message and the id" {
  run env CONSENT_CHECK_REPO_ROOT="$BATS_TEST_TMPDIR" bash "$SCRIPT" test-cap-missing

  [ "$status" -eq 1 ]
  [[ "$output" == *"not active/consented"* ]]
  [[ "$output" == *"test-cap-missing"* ]]
}

@test "present but inactive capability exits 1" {
  run env CONSENT_CHECK_REPO_ROOT="$BATS_TEST_TMPDIR" bash "$SCRIPT" test-cap-inactive

  [ "$status" -eq 1 ]
}

@test "gsd-tools stub exiting non-zero exits 1 and fails closed" {
  cat > "$GSD_TOOLS_DIR/gsd-tools" <<'EOF'
process.exit(3);
EOF

  run env CONSENT_CHECK_REPO_ROOT="$BATS_TEST_TMPDIR" bash "$SCRIPT" test-cap-active

  [ "$status" -eq 1 ]
  [[ "$output" == *"failing closed"* ]]
}

@test "missing gsd-tools binary exits 1 and fails closed" {
  EMPTY_ROOT="$BATS_TEST_TMPDIR/empty-root"
  mkdir -p "$EMPTY_ROOT"

  run env CONSENT_CHECK_REPO_ROOT="$EMPTY_ROOT" bash "$SCRIPT" test-cap-active

  [ "$status" -eq 1 ]
  [[ "$output" == *"failing closed"* ]]
}

@test "zero args exits 2 with usage message" {
  run env CONSENT_CHECK_REPO_ROOT="$BATS_TEST_TMPDIR" bash "$SCRIPT"

  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: consent-check.sh"* ]]
}

@test "two args exits 2 with usage message" {
  run env CONSENT_CHECK_REPO_ROOT="$BATS_TEST_TMPDIR" bash "$SCRIPT" cap-a cap-b

  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: consent-check.sh"* ]]
}
