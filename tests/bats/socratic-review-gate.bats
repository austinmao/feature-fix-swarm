#!/usr/bin/env bats
# socratic-review-gate.bats — spec-005 REQ-07/AC-007/AC-008: arms the
# review-gate honest-verifier pass (skills/review-gate/SKILL.md) with the
# --mode verify slice (Verification blocks + ASSUME ledger) as a third
# delimited block, and drives the per-ASSUME audit.
#
# TWO kinds of case live in this file, named explicitly because they prove
# different things:
#   - EXECUTABLE cases drive the real scripts/gsd/socratic-slice.sh helper
#     directly (in verify mode, against fixture spec dirs) — proving the
#     helper behavior the honest-verifier's wiring depends on.
#   - GREP-PIN cases are static assertions on committed SKILL.md prose
#     (tests/bats/int-plan-wall-skill.bats's pattern) — the honest-verifier
#     pass itself is LLM-followed markdown with no executable prompt
#     assembly, so a bats test can pin WHERE the block goes and WHAT the
#     instructions say, but cannot execute an LLM's verdict logic. A pin is
#     not a behavioral guarantee; naming the boundary here stops a later
#     reader from mistaking one for the other.

bats_require_minimum_version 1.5.0

setup() {
  load 'helpers/socratic-fixtures'
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$REPO_ROOT/scripts/gsd/socratic-slice.sh"
  SKILL="$REPO_ROOT/skills/review-gate/SKILL.md"
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  SPEC="$BATS_TEST_TMPDIR/spec"
  make_vendor_tree "$VENDOR"
}

# --- Task 1: tracer — one armed path, one silent path, one usage-error path -

@test "tracer: verify mode against a spec dir emits Verification blocks and the ASSUME ledger in one delimiter pair" {
  make_spec_dir "$SPEC" "domains: [requirements, security]
depth: core" "- ASSUME-001: default A
- ASSUME-002: default B"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>/dev/null"

  [ "$status" -eq 0 ]
  [[ "$output" == *"VERIFICATION_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" == *"VERIFICATION_SECURITY_SENTINEL"* ]]
  [[ "$output" == *"ASSUME-001: default A"* ]]
  [[ "$output" == *"ASSUME-002: default B"* ]]

  start_count="$(printf '%s\n' "$output" | grep -c '^SOCRATIC_DATA_START$')"
  end_count="$(printf '%s\n' "$output" | grep -c '^SOCRATIC_DATA_END$')"
  [ "$start_count" -eq 1 ]
  [ "$end_count" -eq 1 ]

  start_offset="$(printf '%s' "$output" | grep -bo '^SOCRATIC_DATA_START$' | head -1 | cut -d: -f1)"
  end_offset="$(printf '%s' "$output" | grep -bo '^SOCRATIC_DATA_END$' | head -1 | cut -d: -f1)"
  assume1_offset="$(printf '%s' "$output" | grep -bo 'ASSUME-001: default A' | head -1 | cut -d: -f1)"
  assume2_offset="$(printf '%s' "$output" | grep -bo 'ASSUME-002: default B' | head -1 | cut -d: -f1)"
  [ -n "$start_offset" ]
  [ -n "$end_offset" ]
  [ "$assume1_offset" -gt "$start_offset" ]
  [ "$assume1_offset" -lt "$end_offset" ]
  [ "$assume2_offset" -gt "$start_offset" ]
  [ "$assume2_offset" -lt "$end_offset" ]
}

@test "tracer: an absent vendor tree yields zero bytes" {
  make_spec_dir "$SPEC" "domains: [requirements]"
  local out="$BATS_TEST_TMPDIR/verify-absent.out"

  run bash -c "FFS_SOCRATIC_DIR='$BATS_TEST_TMPDIR/does-not-exist' bash '$SCRIPT' '$SPEC' --mode verify 2>/dev/null > '$out'"

  [ "$status" -eq 0 ]
  [ -f "$out" ]
  [ ! -s "$out" ]
}

@test "a usage error cannot arm" {
  make_spec_dir "$SPEC" "domains: [requirements]"
  local out="$BATS_TEST_TMPDIR/usage.out"
  local err="$BATS_TEST_TMPDIR/usage.err"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode > '$out' 2> '$err'"

  [ "$status" -eq 2 ]
  [ -f "$out" ]
  [ ! -s "$out" ]
  [ -s "$err" ]
  grep -q '^usage: socratic-slice.sh' "$err"
}

@test "the honest verifier invokes the slice helper in verify mode after resolving the spec" {
  spec_line="$(grep -nF '[ -n "$_NNN" ] && HV_SPEC=' "$SKILL" | head -1 | cut -d: -f1)"
  helper_line="$(grep -nF -- '--mode verify' "$SKILL" | head -1 | cut -d: -f1)"
  prompt_line="$(grep -nF '  HV_PROMPT="You are the honest verifier' "$SKILL" | head -1 | cut -d: -f1)"
  [ -n "$spec_line" ]
  [ -n "$helper_line" ]
  [ -n "$prompt_line" ]
  [ "$helper_line" -gt "$spec_line" ]
  [ "$prompt_line" -gt "$helper_line" ]
}

@test "the block is interpolated as a suffix on the spec-data terminator line" {
  line="$(grep -nF 'SPEC_DATA_END${HV_SOCRATIC_BLOCK}' "$SKILL" | head -1 | cut -d: -f1)"
  [ -n "$line" ]
}

@test "the diff block still follows immediately" {
  terminator_line="$(grep -nF 'SPEC_DATA_END${HV_SOCRATIC_BLOCK}' "$SKILL" | head -1 | cut -d: -f1)"
  diff_start_line="$(grep -n '^DIFF_DATA_START$' "$SKILL" | awk -F: -v after="$terminator_line" '$1 > after { print $1; exit }')"
  [ -n "$terminator_line" ]
  [ -n "$diff_start_line" ]
  [ "$diff_start_line" -eq "$((terminator_line + 1))" ]
}

@test "the block variable is initialised empty and filled only when the helper spoke" {
  init_line="$(grep -nF 'HV_SOCRATIC_BLOCK=""' "$SKILL" | head -1 | cut -d: -f1)"
  cond_line="$(grep -nF 'if [ -n "$HV_SOCRATIC_OUT" ]' "$SKILL" | head -1 | cut -d: -f1)"
  [ -n "$init_line" ]
  [ -n "$cond_line" ]
  [ "$cond_line" -gt "$init_line" ]
}

@test "the capture is errexit-safe and re-derives its own repo root" {
  fence_line="$(grep -nF '# resolve the spec for this diff' "$SKILL" | head -1 | cut -d: -f1)"
  reroot_line="$(grep -nF 'HV_REPO_ROOT="$(git rev-parse --show-toplevel' "$SKILL" | head -1 | cut -d: -f1)"
  capture_line="$(grep -nF 'HV_SOCRATIC_OUT="$(bash "$HV_SOCRATIC_HELPER"' "$SKILL" | head -1)"
  capture_line_num="$(printf '%s' "$capture_line" | cut -d: -f1)"
  [ -n "$fence_line" ]
  [ -n "$reroot_line" ]
  [ -n "$capture_line_num" ]
  [ "$reroot_line" -gt "$fence_line" ]
  [ "$capture_line_num" -gt "$reroot_line" ]
  [[ "$capture_line" == *'|| true'* ]]
}

# --- Task 2: the ASSUME audit — per-entry verdicts, zero-entry, routing ----

@test "the ledger reaches verify-mode stdout in declaration order" {
  make_spec_dir "$SPEC" "domains: [requirements]" "- ASSUME-003: third
- ASSUME-001: first
- ASSUME-002: second"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>/dev/null"

  [ "$status" -eq 0 ]
  [[ "$output" == *"ASSUME-003: third"* ]]
  [[ "$output" == *"ASSUME-001: first"* ]]
  [[ "$output" == *"ASSUME-002: second"* ]]

  off3="$(printf '%s' "$output" | grep -bo 'ASSUME-003: third' | head -1 | cut -d: -f1)"
  off1="$(printf '%s' "$output" | grep -bo 'ASSUME-001: first' | head -1 | cut -d: -f1)"
  off2="$(printf '%s' "$output" | grep -bo 'ASSUME-002: second' | head -1 | cut -d: -f1)"
  [ "$off3" -lt "$off1" ]
  [ "$off1" -lt "$off2" ]
}

@test "a spec with Verification content and an empty ledger still arms" {
  make_spec_dir "$SPEC" "domains: [requirements]"

  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$SCRIPT' '$SPEC' --mode verify 2>/dev/null"

  [ "$status" -eq 0 ]
  [ -n "$output" ]
  [[ "$output" == *"VERIFICATION_REQUIREMENTS_SENTINEL"* ]]
  [[ "$output" != *"ASSUME-"* ]]
}

@test "one verdict per ledger entry, in ledger order, in the fixed shape" {
  cond_line="$(grep -nF 'if [ -n "$HV_SOCRATIC_OUT" ]' "$SKILL" | head -1 | cut -d: -f1)"
  block_end_line="$(grep -n '^\$HV_SOCRATIC_OUT"$' "$SKILL" | head -1 | cut -d: -f1)"
  held_line="$(grep -nF 'held|violated|unverifiable' "$SKILL" | head -1 | cut -d: -f1)"
  order_line="$(grep -nF 'in the order the ledger presents them' "$SKILL" | head -1 | cut -d: -f1)"
  shape_line="$(grep -nF 'ASSUME-NNN: held|violated|unverifiable' "$SKILL" | head -1 | cut -d: -f1)"
  [ -n "$cond_line" ]
  [ -n "$block_end_line" ]
  [ -n "$held_line" ]
  [ -n "$order_line" ]
  [ -n "$shape_line" ]
  [ "$held_line" -gt "$cond_line" ]
  [ "$held_line" -lt "$block_end_line" ]
  [ "$order_line" -gt "$cond_line" ]
  [ "$order_line" -lt "$block_end_line" ]
}

@test "the zero-entry wording is present and literal" {
  grep -qF 'ASSUME AUDIT: 0 assumptions recorded' "$SKILL"
}

@test "fabrication is forbidden" {
  grep -qF 'Never produce a verdict for an identifier not present' "$SKILL"
}

@test "violated verdicts are routed into the merged findings at HIGH" {
  routing_line="$(grep -nF '**ASSUME audit routing:**' "$SKILL" | head -1 | cut -d: -f1)"
  merge_line="$(grep -n '^### Merge and rank$' "$SKILL" | head -1 | cut -d: -f1)"
  record_line="$(grep -n '^### Record findings' "$SKILL" | head -1 | cut -d: -f1)"
  [ -n "$routing_line" ]
  [ -n "$merge_line" ]
  [ -n "$record_line" ]
  [ "$routing_line" -lt "$merge_line" ]
  [ "$routing_line" -lt "$record_line" ]
  routing_text="$(sed -n "${routing_line},$((routing_line + 2))p" "$SKILL")"
  [[ "$routing_text" == *"HIGH"* ]]
}

# --- Task 3 (cross-model review fix round): EXECUTABLE case ----------------
# Extracts the ACTUAL shell lines of the HV_SOCRATIC block from SKILL.md
# (sed, anchored on unique substrings rather than hardcoded line numbers) and
# evals them against a stub HV_SPEC/FFS_SOCRATIC_DIR fixture — the GREP-PIN
# cases above prove the prose/placement; this one proves the shell behaves.

build_hv_harness() {
  local out="$1" spec_md="$2" block="$3"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'set -uo pipefail\n'
    printf 'HV_SPEC=%q\n' "$spec_md"
    printf '%s\n' "$block"
    printf 'printf "%%s\\n" "SPEC_DATA_END${HV_SOCRATIC_BLOCK}"\n'
    printf 'printf "DIFF_DATA_START\\n"\n'
  } > "$out"
}

@test "EXECUTABLE: the extracted HV_SOCRATIC block arms exactly once, and is byte-identical unarmed" {
  cd "$REPO_ROOT"
  start_line="$(grep -nF 'HV_SPEC_DIR="$(dirname "$HV_SPEC")"' "$SKILL" | head -1 | cut -d: -f1)"
  fi_line="$(grep -nFx '$HV_SOCRATIC_OUT"' "$SKILL" | head -1 | cut -d: -f1)"
  [ -n "$start_line" ]
  [ -n "$fi_line" ]
  end_line=$((fi_line + 1))
  block_script="$(sed -n "${start_line},${end_line}p" "$SKILL")"
  [[ "$block_script" == *'if [ -n "$HV_SOCRATIC_OUT" ]; then'* ]]

  harness="$BATS_TEST_TMPDIR/hv-socratic-harness.sh"

  # --- armed: vendor tree + socratic.md carrying an ASSUME entry -----------
  make_spec_dir "$SPEC" "domains: [requirements]" "- ASSUME-001: default A"
  build_hv_harness "$harness" "$SPEC/spec.md" "$block_script"
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$harness'"
  [ "$status" -eq 0 ]

  start_count="$(printf '%s\n' "$output" | grep -c '^SOCRATIC_DATA_START$')"
  end_count="$(printf '%s\n' "$output" | grep -c '^SOCRATIC_DATA_END$')"
  [ "$start_count" -eq 1 ]
  [ "$end_count" -eq 1 ]

  spec_end_offset="$(printf '%s' "$output" | grep -bo '^SPEC_DATA_END$' | head -1 | cut -d: -f1)"
  socratic_start_offset="$(printf '%s' "$output" | grep -bo '^SOCRATIC_DATA_START$' | head -1 | cut -d: -f1)"
  diff_start_offset="$(printf '%s' "$output" | grep -bo '^DIFF_DATA_START$' | head -1 | cut -d: -f1)"
  socratic_end_offset="$(printf '%s' "$output" | grep -bo '^SOCRATIC_DATA_END$' | head -1 | cut -d: -f1)"
  [ -n "$spec_end_offset" ]
  [ -n "$socratic_start_offset" ]
  [ -n "$socratic_end_offset" ]
  [ -n "$diff_start_offset" ]
  [ "$socratic_start_offset" -gt "$spec_end_offset" ]
  [ "$socratic_end_offset" -lt "$diff_start_offset" ]

  # --- unarmed: vendor tree present, spec dir has NO socratic.md -----------
  UNARMED_SPEC="$BATS_TEST_TMPDIR/unarmed-spec"
  mkdir -p "$UNARMED_SPEC"
  build_hv_harness "$harness" "$UNARMED_SPEC/spec.md" "$block_script"
  # stdout must stay byte-identical unarmed; the helper's status line now
  # passes through on stderr (AC-012 observability) instead of being dropped
  unarmed_err="$BATS_TEST_TMPDIR/unarmed.err"
  run bash -c "FFS_SOCRATIC_DIR='$VENDOR' bash '$harness' 2>'$unarmed_err'"
  [ "$status" -eq 0 ]

  no_block_expected="$(printf '%s\n' 'SPEC_DATA_END' 'DIFF_DATA_START')"
  [ "$output" = "$no_block_expected" ]
  grep -q '^socratic: skipped' "$unarmed_err"
}

@test "the verifier grammar is untouched" {
  grep -qF 'HV_VERDICT_COUNT=' "$SKILL"
  grep -qF '[ "$HV_VERDICT_COUNT" -ne 1 ]' "$SKILL"
  grep -qF 'ASSUME-NNN:' "$SKILL"
  ! grep -qF 'VERIFIER: ASSUME' "$SKILL"
}
