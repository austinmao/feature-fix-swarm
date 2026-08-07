#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  COORD="$ROOT/scripts/coord/coord.py"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
  git -C "$REPO" init -q -b main
  git -C "$REPO" config user.email t@t
  git -C "$REPO" config user.name t
  git -C "$REPO" commit -q --allow-empty -m init
}

# ── Task 1: end-to-end "claim spec-009" — one path through every layer ─────

@test "claim spec-009 prints session then CLAIM-OK, exits 0, writes generation-1 holder" {
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  [[ "${lines[0]}" == session=* ]]
  [[ "$output" == *"CLAIM-OK generation=1"* ]]
  registry="$REPO/.feature-fix-swarm/coord/registry.json"
  [ -f "$registry" ]
  run python3 -c "import json; d=json.load(open('$registry')); e=d['claims']['claim:spec-009']; print(e['generation'])"
  [ "$status" -eq 0 ]
  [ "$output" = "1" ]
}

@test "status lists spec-009 holder, generation, and expiry after the claiming process is gone" {
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]

  run env -C "$REPO" python3 "$COORD" status
  [ "$status" -eq 0 ]
  [[ "$output" == *"claim:spec-009"* ]]
  [[ "$output" == *"generation=1"* ]]
  [[ "$output" == *"expires_at="* ]]
}

@test "identity round-trips: exporting the emitted session= value resolves the same identity" {
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  session_line="${lines[0]}"
  sid="${session_line#session=}"

  run env -C "$REPO" FFS_COORD_SESSION="$sid" python3 "$COORD" status
  [ "$status" -eq 0 ]
}

@test "coord.py outside any git repository exits 78" {
  outside="$BATS_TEST_TMPDIR/not-a-repo"
  mkdir -p "$outside"
  run env -C "$outside" python3 "$COORD" claim spec-009
  [ "$status" -eq 78 ]
}

@test "malformed spec-id is rejected with exit 2 before any write" {
  run env -C "$REPO" python3 "$COORD" claim "../etc"
  [ "$status" -eq 2 ]
  [ ! -f "$REPO/.feature-fix-swarm/coord/registry.json" ]
}

@test "symlinked store root is refused before any write" {
  mkdir -p "$REPO/.feature-fix-swarm"
  real="$BATS_TEST_TMPDIR/real-coord"
  mkdir -p "$real"
  ln -s "$real" "$REPO/.feature-fix-swarm/coord"
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 78 ]
  [ ! -e "$real/registry.json" ]
}

@test "a by-run pointer with non-uuid content exits 69 naming the pointer, not repaired" {
  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  pointer="$REPO/.feature-fix-swarm/coord/sessions/by-run/r1"
  [ -f "$pointer" ]
  printf 'not-a-uuid' > "$pointer"
  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" claim spec-009
  [ "$status" -eq 69 ]
  [[ "$output" == *"r1"* ]]
  content="$(cat "$pointer")"
  [ "$content" = "not-a-uuid" ]
}
