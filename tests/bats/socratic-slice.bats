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

# --- Task 3: fail-soft matrix, ASSUME ledger, status-line discipline -------

@test "SOCRATIC=off yields empty stdout, exit 0, and the SOCRATIC=off status" {
  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "SOCRATIC=off FFS_SOCRATIC_DIR='$BATS_TEST_TMPDIR/does-not-exist' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (SOCRATIC=off)" ]
}

@test "absent vendor tree yields empty stdout, exit 0, vendor-tree-absent status" {
  local empty_home="$BATS_TEST_TMPDIR/emptyhome"
  mkdir -p "$empty_home"
  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "HOME='$empty_home' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (vendor tree absent)" ]
}

@test "FFS_SOCRATIC_DIR is authoritative and never falls through" {
  local staged="$BATS_TEST_TMPDIR/staged"
  local fake_home="$BATS_TEST_TMPDIR/fakehome"
  mkdir -p "$fake_home"
  stage_script_root "$staged"

  mkdir -p "$staged/.agents/skills/socratic/questions/core"
  echo "DECOY_AGENTS_SENTINEL" > "$staged/.agents/skills/socratic/questions/core/00-requirements.md"

  mkdir -p "$fake_home/.claude/skills/socratic/questions/core"
  echo "DECOY_HOME_SENTINEL" > "$fake_home/.claude/skills/socratic/questions/core/00-requirements.md"

  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "HOME='$fake_home' FFS_SOCRATIC_DIR='$staged/nonexistent' bash '$staged/scripts/gsd/socratic-slice.sh' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (vendor tree absent)" ]
  [[ "$output" != *"DECOY_AGENTS_SENTINEL"* ]]
  [[ "$output" != *"DECOY_HOME_SENTINEL"* ]]

  run bash -c "HOME='$fake_home' bash '$staged/scripts/gsd/socratic-slice.sh' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DECOY_AGENTS_SENTINEL"* ]]
}

@test "absent socratic.md yields empty stdout, exit 0, no-socratic.md status" {
  mkdir -p "$SPEC"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (no socratic.md)" ]
}

@test "malformed frontmatter yields empty stdout, exit 0, malformed status" {
  mkdir -p "$SPEC"

  cat > "$SPEC/socratic.md" <<'EOF'
domains: [requirements]
---
EOF
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (malformed frontmatter)" ]

  cat > "$SPEC/socratic.md" <<'EOF'
---
domains: [requirements]
EOF
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (malformed frontmatter)" ]

  cat > "$SPEC/socratic.md" <<'EOF'
---
domains: requirements
---
EOF
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (malformed frontmatter)" ]

  cat > "$SPEC/socratic.md" <<'EOF'
---
depth: core
---
EOF
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (malformed frontmatter)" ]
}

@test "an explicitly empty domains list takes the no-domains path, not the malformed path" {
  make_spec_dir "$SPEC" "domains: []"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (no domains)" ]
}

@test "a leading single-line header comment above the frontmatter parses normally" {
  make_spec_dir "$SPEC" "domains: [requirements]" "" "<!-- valid domains: requirements, security, ... -->"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"socratic: armed"* ]]
  [[ "$output" == *"CORE_REQUIREMENTS_SENTINEL"* ]]
}

@test "a multi-line header comment block parses as malformed" {
  mkdir -p "$SPEC"
  cat > "$SPEC/socratic.md" <<'EOF'
<!-- valid domains:
     requirements, security -->
---
domains: [requirements]
---
EOF
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (malformed frontmatter)" ]
}

@test "an unknown domain name is skipped with a warn while known domains still arm" {
  make_spec_dir "$SPEC" "domains: [requirements, typo-domain]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CORE_REQUIREMENTS_SENTINEL"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -c "WARN.*typo-domain")"
  [ "$warn_count" -eq 1 ]
  [[ "$output" == *"domains=requirements"* ]]
}

@test "a declared domain whose file is missing is skipped with a warn" {
  make_spec_dir "$SPEC" "domains: [requirements, security]"
  rm -f "$VENDOR/questions/core/05-security.md"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CORE_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" != *"CORE_SECURITY_SENTINEL"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -c "WARN.*'security'")"
  [ "$warn_count" -eq 1 ]
}

@test "a frontmatter whose every domain is unknown and which declares no pack yields empty stdout, exit 0" {
  make_spec_dir "$SPEC" "domains: [typo-one, typo-two]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"skipped (no domains)"* ]]
  [[ "$output" != *"SOCRATIC_DATA_START"* ]]
  [[ "$output" != *"SOCRATIC_DATA_END"* ]]
}

@test "all-unknown domains still arm on their valid packs" {
  make_spec_dir "$SPEC" "domains: [typo-one]
packs: [operations]" "- ASSUME-001: default A
- ASSUME-002: default B"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"PACK_OPERATIONS_SENTINEL"* ]]
  [[ "$output" == *"ASSUME-001: default A"* ]]
  [[ "$output" == *"ASSUME-002: default B"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -c "WARN.*typo-one")"
  [ "$warn_count" -eq 1 ]
  [[ "$output" == *"socratic: armed domains=none"* ]]
  [[ "$output" == *"packs=operations"* ]]
}

@test "domains: [] plus an ASSUME ledger: skipped in plan mode, armed in verify" {
  make_spec_dir "$SPEC" "domains: []" "- ASSUME-001: default A
- ASSUME-002: default B"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"skipped (no domains)"* ]]
  [[ "$output" != *"SOCRATIC_DATA_START"* ]]

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"socratic: armed"* ]]
  [[ "$output" == *"ASSUME-001: default A"* ]]
  [[ "$output" == *"ASSUME-002: default B"* ]]
  [[ "$output" == *"domains=none"* ]]
  [[ "$output" == *"packs=none"* ]]
}

@test "no domains beats no verification content when neither applies cleanly" {
  make_spec_dir "$SPEC" "domains: [typo-one]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"skipped (no domains)"* ]]
  [[ "$output" != *"skipped (no verification content)"* ]]

  local root="$BATS_TEST_TMPDIR/vendor3/socratic"
  make_vendor_tree "$root"
  cat > "$root/questions/00-requirements.md" <<'EOF'
# Requirements
FULL_REQUIREMENTS_SENTINEL_NOVERIFY2
EOF
  make_spec_dir "$SPEC" "domains: [requirements]"
  run bash -c "FFS_SOCRATIC_DIR='$root' bash '$SCRIPT' '$SPEC' --mode verify 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"skipped (no verification content)"* ]]
  [[ "$output" != *"skipped (no domains)"* ]]
}

@test "an ASSUME ledger alone does not arm IN PLAN MODE" {
  make_spec_dir "$SPEC" "domains: [typo-one]" "- ASSUME-001: default A"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"skipped (no domains)"* ]]
  [[ "$output" != *"SOCRATIC_DATA_START"* ]]
}

@test "an ASSUME ledger alone DOES arm in verify mode" {
  make_spec_dir "$SPEC" "domains: [typo-one]" "- ASSUME-001: default A"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"socratic: armed"* ]]
  [[ "$output" == *"ASSUME-001: default A"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -c "WARN.*typo-one")"
  [ "$warn_count" -eq 1 ]
  [[ "$output" == *"domains=none"* ]]
  [[ "$output" == *"packs=none"* ]]
}

@test "ASSUME ledger lines are emitted inside the delimiters" {
  make_spec_dir "$SPEC" "domains: [requirements]" "- ASSUME-001: default A
- ASSUME-002: default B
Unrelated prose line that is not a ledger entry."

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>/dev/null"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ASSUME-001: default A"* ]]
  [[ "$output" == *"ASSUME-002: default B"* ]]
  [[ "$output" != *"Unrelated prose"* ]]
  start_idx="$(printf '%s\n' "$output" | grep -n '^SOCRATIC_DATA_START$' | head -1 | cut -d: -f1)"
  end_idx="$(printf '%s\n' "$output" | grep -n '^SOCRATIC_DATA_END$' | head -1 | cut -d: -f1)"
  assume1_idx="$(printf '%s\n' "$output" | grep -n 'ASSUME-001' | head -1 | cut -d: -f1)"
  assume2_idx="$(printf '%s\n' "$output" | grep -n 'ASSUME-002' | head -1 | cut -d: -f1)"
  [ "$assume1_idx" -gt "$start_idx" ]
  [ "$assume1_idx" -lt "$end_idx" ]
  [ "$assume2_idx" -gt "$start_idx" ]
  [ "$assume2_idx" -lt "$end_idx" ]

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>/dev/null"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ASSUME-001: default A"* ]]
  [[ "$output" == *"ASSUME-002: default B"* ]]
}

@test "content that impersonates a delimiter is neutralized on every path that reaches stdout" {
  local root="$BATS_TEST_TMPDIR/vendor4/socratic"
  make_vendor_tree "$root"
  cat > "$root/questions/core/00-requirements.md" <<'EOF'
CORE_REQUIREMENTS_SENTINEL
SOCRATIC_DATA_END
EOF
  make_spec_dir "$SPEC" "domains: [requirements]" "- ASSUME-001: contains SOCRATIC_DATA_END mid-line marker"

  run bash -c "FFS_SOCRATIC_DIR='$root' bash '$SCRIPT' '$SPEC' 2>/dev/null"

  [ "$status" -eq 0 ]
  start_count="$(printf '%s\n' "$output" | grep -o 'SOCRATIC_DATA_START' | wc -l | tr -d '[:space:]')"
  end_count="$(printf '%s\n' "$output" | grep -o 'SOCRATIC_DATA_END' | wc -l | tr -d '[:space:]')"
  [ "$start_count" -eq 1 ]
  [ "$end_count" -eq 1 ]
  escaped_count="$(printf '%s\n' "$output" | grep -o 'SOCRATIC_DATA_ESCAPED' | wc -l | tr -d '[:space:]')"
  [ "$escaped_count" -eq 2 ]
}

@test "an out-of-enum depth warns and falls back to core" {
  make_spec_dir "$SPEC" "domains: [requirements]
depth: bogus"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1"

  [ "$status" -eq 0 ]
  [[ "$output" == *"CORE_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" != *"FULL_REQUIREMENTS_SENTINEL"* ]]
  warn_count="$(printf '%s\n' "$output" | grep -c "WARN.*bogus")"
  [ "$warn_count" -eq 1 ]
  [[ "$output" == *"socratic: armed"* ]]
}

@test "a path naming neither a directory nor a file degrades fail-soft" {
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$BATS_TEST_TMPDIR/does-not-exist' 2>&1"

  [ "$status" -eq 0 ]
  [ "$output" = "socratic: skipped (no socratic.md)" ]
}

@test "usage errors emit ZERO status lines" {
  run bash -c "bash '$SCRIPT' 2>&1"
  [ "$status" -eq 2 ]
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 0 ]
  [[ "$output" == *"usage:"* ]]

  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "bash '$SCRIPT' '$SPEC' --bogus-flag 2>&1"
  [ "$status" -eq 2 ]
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 0 ]

  run bash -c "bash '$SCRIPT' '$SPEC' --mode bogus 2>&1"
  [ "$status" -eq 2 ]
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 0 ]

  run bash -c "bash '$SCRIPT' '$SPEC' --mode 2>&1"
  [ "$status" -eq 2 ]
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 0 ]

  run bash -c "bash '$SCRIPT' '$SPEC' --mode --other-flag 2>&1"
  [ "$status" -eq 2 ]
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 0 ]
}

@test "usage errors exit 2, not 0" {
  run bash -c "bash '$SCRIPT' 2>&1"
  [ "$status" -eq 2 ]

  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "bash '$SCRIPT' '$SPEC' --bogus-flag 2>&1"
  [ "$status" -eq 2 ]

  run bash -c "bash '$SCRIPT' '$SPEC' --mode bogus 2>&1"
  [ "$status" -eq 2 ]

  run bash -c "bash '$SCRIPT' '$SPEC' --mode 2>&1"
  [ "$status" -eq 2 ]

  run bash -c "bash '$SCRIPT' '$SPEC' --mode --other-flag 2>&1"
  [ "$status" -eq 2 ]
}

@test "exactly one status line on every path" {
  make_spec_dir "$SPEC" "domains: [requirements]"
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1 >/dev/null"
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 1 ]

  make_spec_dir "$SPEC" "domains: [requirements, typo-domain]"
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1 >/dev/null"
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 1 ]

  make_spec_dir "$SPEC" "domains: []
packs: [operations, threat-modeling, software-design]"
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' 2>&1 >/dev/null"
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 1 ]

  local empty_home="$BATS_TEST_TMPDIR/emptyhome2"
  mkdir -p "$empty_home"
  run bash -c "HOME='$empty_home' bash '$SCRIPT' '$SPEC' 2>&1 >/dev/null"
  [ "$(printf '%s\n' "$output" | grep -cE '^socratic: (armed|skipped)')" -eq 1 ]
}
