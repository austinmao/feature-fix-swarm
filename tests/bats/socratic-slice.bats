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

# --- Task 2: depth, verify mode, pack cap -----------------------------------

@test "depth core reads only the core files" {
  make_spec_dir "$SPEC" "domains: [requirements, security]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>/dev/null"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CORE_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" == *"CORE_SECURITY_SENTINEL"* ]]
  [[ "$output" != *"FULL_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" != *"FULL_SECURITY_SENTINEL"* ]]
}

@test "depth=full switches to the top-level full question files" {
  make_spec_dir "$SPEC" "domains: [requirements, security]
depth: full"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>/dev/null"

  [ "$status" -eq 0 ]
  [[ "$output" == *"FULL_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" == *"FULL_SECURITY_SENTINEL"* ]]
  [[ "$output" != *"CORE_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" != *"CORE_SECURITY_SENTINEL"* ]]
}

@test "mode verify emits only the Verification block from the full files" {
  make_spec_dir "$SPEC" "domains: [requirements, security]
depth: core"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>/dev/null"

  [ "$status" -eq 0 ]
  [[ "$output" == *"VERIFICATION_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" == *"VERIFICATION_SECURITY_SENTINEL"* ]]
  [[ "$output" != *"SOMETHING_ELSE_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" != *"SOMETHING_ELSE_SECURITY_SENTINEL"* ]]
  [[ "$output" != *"CORE_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" != *"CORE_SECURITY_SENTINEL"* ]]
}

@test "mode plan and mode arm are accepted synonyms of the default" {
  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>/dev/null"
  [ "$status" -eq 0 ]
  default_out="$output"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode plan 2>/dev/null"
  [ "$status" -eq 0 ]
  plan_out="$output"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode arm 2>/dev/null"
  [ "$status" -eq 0 ]
  arm_out="$output"

  [ "$default_out" = "$plan_out" ]
  [ "$default_out" = "$arm_out" ]
}

@test "an in-enum pack whose file is missing never consumes a cap slot" {
  make_spec_dir "$SPEC" "domains: []
packs: [operations, threat-modeling, software-design]"
  rm -f "$VENDOR/packs/operations/core.md"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"PACK_THREAT_MODELING_SENTINEL"* ]]
  [[ "$output" == *"PACK_SOFTWARE_DESIGN_SENTINEL"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -c 'WARN.*operations')"
  [ "$warn_count" -eq 1 ]
}

@test "an unknown pack name never consumes a cap slot" {
  make_spec_dir "$SPEC" "domains: []
packs: [typo-pack, threat-modeling, software-design]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"PACK_THREAT_MODELING_SENTINEL"* ]]
  [[ "$output" == *"PACK_SOFTWARE_DESIGN_SENTINEL"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -c 'WARN.*typo-pack')"
  [ "$warn_count" -eq 1 ]
}

@test "packs are capped at two and always resolve to core.md" {
  make_spec_dir "$SPEC" "domains: []
packs: [operations, threat-modeling, software-design]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"PACK_OPERATIONS_SENTINEL"* ]]
  [[ "$output" == *"PACK_THREAT_MODELING_SENTINEL"* ]]
  [[ "$output" != *"PACK_SOFTWARE_DESIGN_SENTINEL"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -c 'WARN.*software-design')"
  [ "$warn_count" -eq 1 ]
  [[ "$output" == *"socratic: armed"*"packs=operations,threat-modeling"* ]]
}

@test "verify mode carries no pack cards and reports packs=none" {
  make_spec_dir "$SPEC" "domains: [requirements]
packs: [operations]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"VERIFICATION_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" != *"PACK_OPERATIONS_SENTINEL"* ]]
  [[ "$output" == *"socratic: armed"* ]]
  [[ "$output" == *"packs=none"* ]]
}

@test "verify mode with nothing to verify emits nothing" {
  local root="$BATS_TEST_TMPDIR/vendor2/socratic"
  make_vendor_tree "$root"
  cat > "$root/questions/00-requirements.md" <<'EOF'
# Requirements
FULL_REQUIREMENTS_SENTINEL_NOVERIFY
EOF
  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "FFS_SOCRATIC_DIR='$root' bash '$SCRIPT' '$SPEC' --mode verify 2>&1"

  [ "$status" -eq 0 ]
  [ -z "$(printf '%s\n' "$output" | grep -v '^socratic:')" ]
  [[ "$output" == *"skipped (no verification content)"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -cE '^socratic: WARN')"
  [ "$warn_count" -eq 1 ]
  [[ "$output" != *"SOCRATIC_DATA_START"* ]]
  [[ "$output" != *"SOCRATIC_DATA_END"* ]]
}

@test "domain emission order is canonical, not declaration order" {
  make_spec_dir "$SPEC" "domains: [testing, requirements]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>/dev/null"

  [ "$status" -eq 0 ]
  req_offset="$(printf '%s' "$output" | grep -bo 'CORE_REQUIREMENTS_SENTINEL' | head -1 | cut -d: -f1)"
  test_offset="$(printf '%s' "$output" | grep -bo 'CORE_TESTING_SENTINEL' | head -1 | cut -d: -f1)"
  [ -n "$req_offset" ]
  [ -n "$test_offset" ]
  [ "$req_offset" -lt "$test_offset" ]
}

@test "permuted DOMAIN declarations produce byte-identical output" {
  local spec_a="$BATS_TEST_TMPDIR/spec-a"
  local spec_b="$BATS_TEST_TMPDIR/spec-b"
  make_spec_dir "$spec_a" "domains: [requirements, security, testing]
packs: [operations]"
  make_spec_dir "$spec_b" "domains: [testing, requirements, security]
packs: [operations]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$spec_a' 2>/dev/null > '$BATS_TEST_TMPDIR/out-a.txt'"
  [ "$status" -eq 0 ]
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$spec_b' 2>/dev/null > '$BATS_TEST_TMPDIR/out-b.txt'"
  [ "$status" -eq 0 ]

  run cmp "$BATS_TEST_TMPDIR/out-a.txt" "$BATS_TEST_TMPDIR/out-b.txt"
  [ "$status" -eq 0 ]
}
