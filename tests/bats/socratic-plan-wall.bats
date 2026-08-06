#!/usr/bin/env bats
# socratic-plan-wall.bats — spec-005 REQ-05/AC-005/AC-008: arms
# scripts/gsd/plan-wall.sh's reviewer prompt with the Phase 2 slice helper
# (scripts/gsd/socratic-slice.sh). tests/bats/plan-wall.bats:1-59's
# fixture-repo/stub-CLI conventions apply here too — the LEVER under test
# (plan-wall.sh) is never mocked, only the reviewer CLI is.
#
# Regenerate tests/bats/socratic-plan-wall-baseline.prompt ONLY after an
# INTENTIONAL PW_REVIEW_BRIEF change, never to "fix" this feature's own
# output — the baseline is the oracle armed/unarmed cases are graded
# against, so an out-of-order regeneration would make it agree with
# whatever the change produced, including a wrong prompt:
#   1. checkout an UNMODIFIED scripts/gsd/plan-wall.sh (git stash / git show)
#   2. run this file's "an unarmed run is byte-identical to the captured
#      baseline" case, which leaves $PROMPT_CAPTURE populated
#   3. cp "$PROMPT_CAPTURE" tests/bats/socratic-plan-wall-baseline.prompt

bats_require_minimum_version 1.5.0

setup() {
  load 'helpers/socratic-fixtures'
  LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/plan-wall.sh"
  REAL_GATES_PY="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/lib/gates.py"
  REAL_SCHEMA="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/schemas/review-finding.schema.json"
  BASELINE="$(cd "$BATS_TEST_DIRNAME" && pwd)/socratic-plan-wall-baseline.prompt"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/packages/feature-fix-swarm/lib" "$REPO/schemas" "$REPO/bin"
  cp "$REAL_GATES_PY" "$REPO/packages/feature-fix-swarm/lib/gates.py"
  cp "$REAL_SCHEMA" "$REPO/schemas/review-finding.schema.json"
  cd "$REPO"
  git init -q -b main
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  mkdir -p .planning/phases/1-foo
  cat > .planning/config.json <<JSON
{"model_overrides": {"gsd-planner": "fable"}, "dynamic_routing": {"escalate_on_failure": true}}
JSON
  echo "Phase 1: build a plain widget, nothing sensitive here" > .planning/phases/1-foo/PLAN.md
  export PATH="$REPO/bin:$PATH"
  export FFS_ADVERSARY_MODEL_PROBE=off
  export GATES_PY="$REPO/packages/feature-fix-swarm/lib/gates.py"
  # Other vendor's binary resolves nowhere, so rule-1 opposite-vendor fails
  # fast (rc=127) instead of shelling out to a real CLI on the machine.
  export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz
  export ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz
  PROMPT_CAPTURE="$BATS_TEST_TMPDIR/captured.prompt"
  export PROMPT_CAPTURE
  # Absent by default — points nowhere unless a test builds a vendor tree
  # and overrides this. This is what makes "no vendor tree" the baseline
  # setup rather than something every unarmed case must construct itself.
  export FFS_SOCRATIC_DIR="$BATS_TEST_TMPDIR/no-such-vendor-tree"
}

# capture_stub — tests/bats/plan-wall.bats:49-56's stub_claude_json VERBATIM
# with exactly one change: `cat >/dev/null` becomes `cat > "$PROMPT_CAPTURE"`.
# Same shebang, same response envelope (empty findings array, which
# _pw_validate_findings already accepts), same chmod.
capture_stub() {
  cat > bin/stub-claude <<EOF
#!/usr/bin/env bash
cat > "$PROMPT_CAPTURE"
printf '%s\n' '[]'
EOF
  chmod +x bin/stub-claude
  export ADVERSARY_BIN_CLAUDE="$REPO/bin/stub-claude"
}

# run_wall_capture — runs the wall once against .planning/phases/1-foo and
# leaves the captured prompt at $PROMPT_CAPTURE ($status/$output from `run`).
run_wall_capture() {
  capture_stub
  run bash "$LEVER" .planning/phases/1-foo
}

# --- Task 1: one armed path, one unarmed path -------------------------------

@test "tracer: an armed run appends the slice after the plan block" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-tracer" "domains: [requirements]"
  git checkout -q -b 042-tracer

  run_wall_capture
  [ "$status" -eq 0 ]

  captured="$(cat "$PROMPT_CAPTURE")"
  [[ "$captured" == *"CORE_REQUIREMENTS_SENTINEL"* ]]

  start_count="$(grep -c '^SOCRATIC_DATA_START$' "$PROMPT_CAPTURE")"
  end_count="$(grep -c '^SOCRATIC_DATA_END$' "$PROMPT_CAPTURE")"
  [ "$start_count" -eq 1 ]
  [ "$end_count" -eq 1 ]

  plan_end_offset="$(grep -bo '^PLAN_DATA_END$' "$PROMPT_CAPTURE" | head -1 | cut -d: -f1)"
  socratic_start_offset="$(grep -bo '^SOCRATIC_DATA_START$' "$PROMPT_CAPTURE" | head -1 | cut -d: -f1)"
  [ -n "$plan_end_offset" ]
  [ -n "$socratic_start_offset" ]
  [ "$socratic_start_offset" -gt "$plan_end_offset" ]
}

@test "tracer: the slice is never the last thing the reviewer reads" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-tracer" "domains: [requirements]"
  git checkout -q -b 042-tracer

  run_wall_capture
  [ "$status" -eq 0 ]

  final_line="$(tail -n1 "$PROMPT_CAPTURE")"
  [ "$final_line" != "SOCRATIC_DATA_END" ]
  [ -n "$final_line" ]
  [[ "$final_line" != SOCRATIC_DATA* ]]
}

@test "tracer: an unarmed run is byte-identical to the captured baseline" {
  # FFS_SOCRATIC_DIR (set in setup()) points nowhere -> unarmed.
  run_wall_capture
  [ "$status" -eq 0 ]
  cmp "$PROMPT_CAPTURE" "$BASELINE"
}

# --- Task 2: the four unarmed routes + the idempotence-key fold ------------

@test "a branch with no leading three digits leaves the prompt byte-identical" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-tracer" "domains: [requirements]"
  git checkout -q -b feature-no-digits

  run_wall_capture
  [ "$status" -eq 0 ]
  cmp "$PROMPT_CAPTURE" "$BASELINE"

  rm -rf .planning/run-state
  git checkout -q --detach HEAD
  run_wall_capture
  [ "$status" -eq 0 ]
  cmp "$PROMPT_CAPTURE" "$BASELINE"
}

@test "SOCRATIC=off leaves the prompt byte-identical" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-tracer" "domains: [requirements]"
  git checkout -q -b 042-tracer
  export SOCRATIC=off

  run_wall_capture
  [ "$status" -eq 0 ]
  cmp "$PROMPT_CAPTURE" "$BASELINE"
}

@test "a resolvable spec dir with no socratic.md leaves the prompt byte-identical" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  mkdir -p specs/042-tracer
  git checkout -q -b 042-tracer

  run_wall_capture
  [ "$status" -eq 0 ]
  cmp "$PROMPT_CAPTURE" "$BASELINE"
}

@test "GSD_RUN_ID set still arms" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-tracer" "domains: [requirements]"
  git checkout -q -b 042-tracer
  export GSD_RUN_ID=custom-run-id

  run_wall_capture
  [ "$status" -eq 0 ]
  grep -q 'CORE_REQUIREMENTS_SENTINEL' "$PROMPT_CAPTURE"
}

@test "two spec dirs sharing the numeric prefix resolve to the LEXICALLY first" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-beta" "domains: [security]"
  make_spec_dir "specs/042-alpha" "domains: [requirements]"
  git checkout -q -b 042-tracer

  run_wall_capture
  [ "$status" -eq 0 ]
  grep -q 'CORE_REQUIREMENTS_SENTINEL' "$PROMPT_CAPTURE"
  ! grep -q 'CORE_SECURITY_SENTINEL' "$PROMPT_CAPTURE"
}

@test "editing socratic.md invalidates the zero-dispatch fast path" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-tracer" "domains: [requirements]"
  git checkout -q -b 042-tracer

  run_wall_capture
  [ "$status" -eq 0 ]

  run_wall_capture
  [ "$status" -eq 0 ]
  [[ "$output" == *"ADJUDICATED-PASS"* ]]

  rm -f "$PROMPT_CAPTURE"
  make_spec_dir "specs/042-tracer" "domains: [requirements, security]"
  run_wall_capture
  [ "$status" -eq 0 ]
  [[ "$output" != *"ADJUDICATED-PASS"* ]]
  [ -f "$PROMPT_CAPTURE" ]
}

@test "an unreadable socratic.md neither arms nor folds" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-tracer" "domains: [requirements]"
  chmod 000 specs/042-tracer/socratic.md
  git checkout -q -b 042-tracer

  run_wall_capture
  chmod 644 specs/042-tracer/socratic.md
  [ "$status" -eq 0 ]
  cmp "$PROMPT_CAPTURE" "$BASELINE"

  key="$(jq -r '.plan_sha256' .planning/run-state/plan-wall-1-foo-plan.json)"
  [[ "$key" != *:* ]]
}

@test "the pack cap survives the integration" {
  VENDOR="$BATS_TEST_TMPDIR/vendor/socratic"
  make_vendor_tree "$VENDOR"
  export FFS_SOCRATIC_DIR="$VENDOR"
  make_spec_dir "specs/042-tracer" "domains: [requirements]
packs: [operations, threat-modeling, software-design]"
  git checkout -q -b 042-tracer

  run_wall_capture
  [ "$status" -eq 0 ]
  grep -q 'PACK_OPERATIONS_SENTINEL' "$PROMPT_CAPTURE"
  grep -q 'PACK_THREAT_MODELING_SENTINEL' "$PROMPT_CAPTURE"
  ! grep -q 'PACK_SOFTWARE_DESIGN_SENTINEL' "$PROMPT_CAPTURE"
}

@test "an unarmed run stores the plain plan sha" {
  # FFS_SOCRATIC_DIR (setup()) points nowhere -> unarmed.
  run_wall_capture
  [ "$status" -eq 0 ]
  key="$(jq -r '.plan_sha256' .planning/run-state/plan-wall-1-foo-plan.json)"
  if command -v sha256sum >/dev/null 2>&1; then
    expected="$(sha256sum .planning/phases/1-foo/PLAN.md | awk '{print $1}')"
  else
    expected="$(shasum -a 256 .planning/phases/1-foo/PLAN.md | awk '{print $1}')"
  fi
  [ "$key" = "$expected" ]
}
