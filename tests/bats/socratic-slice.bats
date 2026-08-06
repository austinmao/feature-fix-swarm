#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/socratic-slice.sh"
  load 'helpers/socratic-fixtures'
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  SPEC="$BATS_TEST_TMPDIR/spec"
  make_vendor_tree "$VENDOR"
}

@test "tracer: one declared domain emits its core file inside one delimiter pair" {
  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>/dev/null"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CORE_REQUIREMENTS_SENTINEL"* ]]

  first_line="$(printf '%s\n' "$output" | head -n1)"
  last_line="$(printf '%s\n' "$output" | tail -n1)"
  [ "$first_line" = "SOCRATIC_DATA_START" ]
  [ "$last_line" = "SOCRATIC_DATA_END" ]

  start_count="$(printf '%s\n' "$output" | grep -c '^SOCRATIC_DATA_START$')"
  end_count="$(printf '%s\n' "$output" | grep -c '^SOCRATIC_DATA_END$')"
  [ "$start_count" -eq 1 ]
  [ "$end_count" -eq 1 ]
}

@test "tracer: the invocation emits exactly one status line" {
  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1 >/dev/null"

  [ "$status" -eq 0 ]
  count="$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')"
  [ "$count" -eq 1 ]
  [[ "$output" == *"requirements"* ]]
}
