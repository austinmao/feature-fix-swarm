#!/usr/bin/env bats
# gsd-checkpoint.sh (PostToolUse writer) + gsd-handoff-resume.sh (SessionStart
# surfacer) — microcompact-surviving session continuity (borrowed: buildomator).
# Claude Code's microcompact strips tool outputs WITHOUT firing PreCompact, and
# read-heavy phases write no files — so a PreCompact-only checkpoint can be
# arbitrarily stale. The writer refreshes .planning/run-state/HANDOFF.json at
# most once per 60s; the surfacer prints a resume pointer at session start.
# Both fail-soft: ALWAYS exit 0. Inert until a consumer wires them into
# settings.json — presence in this repo runs nothing.

setup() {
  HOOKS="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/hooks"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/.planning"
  cd "$REPO"
  git init -q -b main
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  printf '# STATE\n\n## Position: phase 2, plan 02-01\n' > .planning/STATE.md
  HANDOFF=".planning/run-state/HANDOFF.json"
}

@test "writer creates HANDOFF.json with branch, head, state head, timestamp" {
  run bash "$HOOKS/gsd-checkpoint.sh"
  [ "$status" -eq 0 ]
  [ -f "$HANDOFF" ]
  grep -q '"branch": *"main"' "$HANDOFF"
  grep -q '"head"' "$HANDOFF"
  grep -q 'phase 2, plan 02-01' "$HANDOFF"
  grep -q '"ts"' "$HANDOFF"
}

@test "throttle: a fresh HANDOFF.json is not rewritten" {
  bash "$HOOKS/gsd-checkpoint.sh"
  before="$(cat "$HANDOFF")"
  printf '# STATE\n\n## Position: phase 3\n' > .planning/STATE.md
  run bash "$HOOKS/gsd-checkpoint.sh"
  [ "$status" -eq 0 ]
  [ "$(cat "$HANDOFF")" = "$before" ]
}

@test "stale HANDOFF.json is rewritten" {
  bash "$HOOKS/gsd-checkpoint.sh"
  touch -t 202601010000 "$HANDOFF"
  printf '# STATE\n\n## Position: phase 3\n' > .planning/STATE.md
  run bash "$HOOKS/gsd-checkpoint.sh"
  [ "$status" -eq 0 ]
  grep -q 'phase 3' "$HANDOFF"
}

@test "no .planning: silent no-op, exit 0" {
  rm -rf .planning
  run bash "$HOOKS/gsd-checkpoint.sh"
  [ "$status" -eq 0 ]
  [ ! -e "$HANDOFF" ]
}

@test "kill-switch GSD_CHECKPOINT=off: no write, exit 0" {
  run env GSD_CHECKPOINT=off bash "$HOOKS/gsd-checkpoint.sh"
  [ "$status" -eq 0 ]
  [ ! -f "$HANDOFF" ]
}

@test "surfacer prints resume pointer when a handoff exists" {
  bash "$HOOKS/gsd-checkpoint.sh"
  run bash "$HOOKS/gsd-handoff-resume.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"HANDOFF"* ]]
  [[ "$output" == *"gsd-resume-work"* ]]
}

@test "surfacer is silent with no handoff, exit 0" {
  run bash "$HOOKS/gsd-handoff-resume.sh"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}
