#!/usr/bin/env bats
# sync-drift-check.sh — vendor-drift detector with an auditable fork allowlist
# (borrowed: buildomator check-drift ratchet pattern; generalizes FALLBACK-017's
# single-file check to the whole scripts/gsd surface). Compares every packaged
# lever against a consumer repo's installed copy:
#   identical -> IN-SYNC   missing -> MISSING (warn, not fail)
#   differs + allowlisted -> FORKED (info)   differs unlisted -> DRIFT (fail)

LEVER="sync-drift-check.sh"

setup() {
  SCRIPTS="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd"
  SRC="$BATS_TEST_TMPDIR/src"
  CONSUMER="$BATS_TEST_TMPDIR/consumer"
  mkdir -p "$SRC" "$CONSUMER"
  printf '#!/bin/bash\necho a\n' > "$SRC/alpha.sh"
  printf '#!/bin/bash\necho b\n' > "$SRC/beta.sh"
  printf 'print("helper")\n' > "$SRC/helper.py"
  cp "$SRC/alpha.sh" "$CONSUMER/alpha.sh"
  cp "$SRC/beta.sh" "$CONSUMER/beta.sh"
  cp "$SRC/helper.py" "$CONSUMER/helper.py"
}

run_check() {
  run env GSD_SYNC_SRC="$SRC" bash "$SCRIPTS/$LEVER" "$CONSUMER" "$@"
}

@test "all identical: exit 0, every file IN-SYNC" {
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"IN-SYNC"* ]]
  [[ "$output" != *"DRIFT"* ]]
}

@test "unlisted drift: exit 1, names the file" {
  echo extra >> "$CONSUMER/beta.sh"
  run_check
  [ "$status" -eq 1 ]
  [[ "$output" == *"DRIFT: beta.sh"* ]]
}

@test "python runner helper drift is fail-closed" {
  echo '# changed' >> "$CONSUMER/helper.py"
  run_check
  [ "$status" -eq 1 ]
  [[ "$output" == *"DRIFT: helper.py"* ]]
}

@test "allowlisted fork: exit 0, reported FORKED with reason" {
  echo extra >> "$CONSUMER/beta.sh"
  printf 'beta.sh consumer keeps a zsh guard\n' > "$BATS_TEST_TMPDIR/allow.txt"
  run_check --allowlist "$BATS_TEST_TMPDIR/allow.txt"
  [ "$status" -eq 0 ]
  [[ "$output" == *"FORKED: beta.sh"* ]]
  [[ "$output" == *"zsh guard"* ]]
}

@test "missing consumer copy: warn MISSING, exit 0" {
  rm "$CONSUMER/alpha.sh"
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"MISSING: alpha.sh"* ]]
}

@test "allowlisted-but-identical file flagged stale allowlist entry" {
  printf 'beta.sh reason gone\n' > "$BATS_TEST_TMPDIR/allow.txt"
  run_check --allowlist "$BATS_TEST_TMPDIR/allow.txt"
  [ "$status" -eq 0 ]
  [[ "$output" == *"STALE-ALLOWLIST: beta.sh"* ]]
}

@test "missing consumer dir is a loud usage error" {
  run env GSD_SYNC_SRC="$SRC" bash "$SCRIPTS/$LEVER" "$BATS_TEST_TMPDIR/nope"
  [ "$status" -ne 0 ]
}
