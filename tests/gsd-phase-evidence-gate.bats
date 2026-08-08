#!/usr/bin/env bats
# TDD RED for scripts/hooks/gsd-phase-evidence-gate.sh — PreToolUse (Edit|Write)
# gsd analog of openclaw's checkbox-evidence-gate.sh: a phase-complete flip in
# .planning/ROADMAP.md or a completed_phases increment in .planning/STATE.md
# requires gates.py verify-done evidence keyed by phase-dir basename or the
# legacy gsd-phase-NN id. Generic `gsd-phase` no longer unlocks (P4-W6).

setup() {
  GATE="$BATS_TEST_DIRNAME/../scripts/hooks/gsd-phase-evidence-gate.sh"
  export GATES_STORE="$BATS_TEST_TMPDIR/evidence.json"
}

hook_input() { # $1=tool $2=path $3=old $4=new
  python3 -c 'import json,sys; print(json.dumps({"tool_name":sys.argv[1],"tool_input":{"file_path":sys.argv[2],"old_string":sys.argv[3],"new_string":sys.argv[4]}}))' "$1" "$2" "$3" "$4"
}

@test "non-planning file passes through" {
  run bash "$GATE" <<EOF
$(hook_input Edit src/foo.md "a" "b")
EOF
  [ "$status" -eq 0 ]
}

@test "ROADMAP phase flip WITHOUT evidence blocks (exit 2)" {
  run bash "$GATE" <<EOF
$(hook_input Edit .planning/ROADMAP.md "- [ ] **Phase 1: Support scripts**" "- [x] **Phase 1: Support scripts**")
EOF
  [ "$status" -eq 2 ]
  [[ "$output" == *"BLOCKED"* ]]
}

@test "ROADMAP phase flip WITH per-phase evidence passes" {
  python3 "$BATS_TEST_DIRNAME/../lib/gates.py" run-gate gsd-phase-1 -- true
  run bash "$GATE" <<EOF
$(hook_input Edit .planning/ROADMAP.md "- [ ] **Phase 1: Support scripts**" "- [x] **Phase 1: Support scripts**")
EOF
  [ "$status" -eq 0 ]
}

@test "generic gsd-phase evidence no longer unlocks a flip (P4-W6)" {
  python3 "$BATS_TEST_DIRNAME/../lib/gates.py" run-gate gsd-phase -- true
  run bash "$GATE" <<EOF
$(hook_input Edit .planning/ROADMAP.md "- [ ] **Phase 2: Next**" "- [x] **Phase 2: Next**")
EOF
  [ "$status" -eq 2 ]
  [[ "$output" == *"BLOCKED"* ]]
}

@test "ROADMAP phase flip WITH phase-dir-basename evidence passes" {
  mkdir -p "$BATS_TEST_TMPDIR/.planning/phases/02-next-things"
  python3 "$BATS_TEST_DIRNAME/../lib/gates.py" run-gate 02-next-things -- true
  run bash "$GATE" <<EOF
$(hook_input Edit "$BATS_TEST_TMPDIR/.planning/ROADMAP.md" "- [ ] **Phase 2: Next**" "- [x] **Phase 2: Next**")
EOF
  [ "$status" -eq 0 ]
}

@test "another phase's dir-basename evidence does NOT unlock this phase" {
  mkdir -p "$BATS_TEST_TMPDIR/.planning/phases/01-other"
  mkdir -p "$BATS_TEST_TMPDIR/.planning/phases/02-next-things"
  python3 "$BATS_TEST_DIRNAME/../lib/gates.py" run-gate 01-other -- true
  run bash "$GATE" <<EOF
$(hook_input Edit "$BATS_TEST_TMPDIR/.planning/ROADMAP.md" "- [ ] **Phase 2: Next**" "- [x] **Phase 2: Next**")
EOF
  [ "$status" -eq 2 ]
}

@test "STATE.md completed_phases increment WITHOUT evidence blocks" {
  run bash "$GATE" <<EOF
$(hook_input Edit .planning/STATE.md "completed_phases: 0" "completed_phases: 1")
EOF
  [ "$status" -eq 2 ]
}

@test "ROADMAP edit with no flip passes" {
  run bash "$GATE" <<EOF
$(hook_input Edit .planning/ROADMAP.md "some prose" "other prose")
EOF
  [ "$status" -eq 0 ]
}

@test "GATES_BYPASS=1 passes without evidence" {
  GATES_BYPASS=1 run bash "$GATE" <<EOF
$(hook_input Edit .planning/ROADMAP.md "- [ ] **Phase 3: X**" "- [x] **Phase 3: X**")
EOF
  [ "$status" -eq 0 ]
}
