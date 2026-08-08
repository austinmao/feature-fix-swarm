#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/coord/forbidden-paths-check.sh"
}

# Light fixture shape of coord-claim.bats:5-14 -- a git init with a seed
# commit, then a branch. Each @test builds its OWN repo (never shared) and
# seeds the three forbidden paths as real files on the base commit, then
# diverges.
mk_repo() {
  local dir="$1"
  mkdir -p "$dir/lib" "$dir/scripts/gsd"
  git -C "$dir" init -q -b main
  git -C "$dir" config user.email t@t
  git -C "$dir" config user.name t
  printf 'seed\n' > "$dir/lib/gates.py"
  printf 'seed\n' > "$dir/scripts/gsd/plan-wall.sh"
  printf 'seed\n' > "$dir/scripts/gsd/run-finalizer.sh"
  printf 'seed\n' > "$dir/README.md"
  git -C "$dir" add -A
  git -C "$dir" commit -qm seed
  git -C "$dir" checkout -qb feature
}

@test "a clean branch exits 0 and prints a PASS line to stdout" {
  REPO="$BATS_TEST_TMPDIR/repo1"
  mk_repo "$REPO"

  run env -C "$REPO" bash "$SCRIPT" --base main
  [ "$status" -eq 0 ]
  [[ "$output" == *PASS* ]]
}

@test "a COMMITTED edit to the gates library on the branch exits 2 and names that path" {
  REPO="$BATS_TEST_TMPDIR/repo2"
  mk_repo "$REPO"
  printf 'tampered\n' >> "$REPO/lib/gates.py"
  git -C "$REPO" commit -qam tamper

  run env -C "$REPO" bash "$SCRIPT" --base main
  [ "$status" -eq 2 ]
  [[ "$output" == *"lib/gates.py"* ]]
}

@test "a STAGED-but-never-committed edit to the plan wall script exits 2" {
  REPO="$BATS_TEST_TMPDIR/repo3"
  mk_repo "$REPO"
  printf 'tampered\n' >> "$REPO/scripts/gsd/plan-wall.sh"
  git -C "$REPO" add "$REPO/scripts/gsd/plan-wall.sh"

  run env -C "$REPO" bash "$SCRIPT" --base main
  [ "$status" -eq 2 ]
  [[ "$output" == *"scripts/gsd/plan-wall.sh"* ]]
}

@test "an UNSTAGED working-tree edit to the run finalizer script exits 2" {
  REPO="$BATS_TEST_TMPDIR/repo4"
  mk_repo "$REPO"
  printf 'tampered\n' >> "$REPO/scripts/gsd/run-finalizer.sh"

  run env -C "$REPO" bash "$SCRIPT" --base main
  [ "$status" -eq 2 ]
  [[ "$output" == *"scripts/gsd/run-finalizer.sh"* ]]
}

@test "a committed git mv of the gates library to a permitted name exits 2, matched on the OLD name" {
  REPO="$BATS_TEST_TMPDIR/repo5"
  mk_repo "$REPO"
  git -C "$REPO" mv lib/gates.py lib/gates_renamed.py
  git -C "$REPO" commit -qm rename

  run env -C "$REPO" bash "$SCRIPT" --base main
  [ "$status" -eq 2 ]
  [[ "$output" == *"lib/gates.py"* ]]
}

@test "a real file whose path merely CONTAINS a forbidden path as a suffix exits 0" {
  REPO="$BATS_TEST_TMPDIR/repo6"
  mk_repo "$REPO"
  mkdir -p "$REPO/vendor/lib"
  printf 'seed\n' > "$REPO/vendor/lib/gates.py"
  git -C "$REPO" add "$REPO/vendor/lib/gates.py"
  git -C "$REPO" commit -qm vendor

  run env -C "$REPO" bash "$SCRIPT" --base main
  [ "$status" -eq 0 ]
}

@test "a forbidden path is still matched when the repository directory contains a space" {
  REPO="$BATS_TEST_TMPDIR/repo with space"
  mk_repo "$REPO"
  printf 'tampered\n' >> "$REPO/lib/gates.py"
  git -C "$REPO" commit -qam tamper

  run env -C "$REPO" bash "$SCRIPT" --base main
  [ "$status" -eq 2 ]
  [[ "$output" == *"lib/gates.py"* ]]
}

@test "an explicit base override that RESOLVES is honored -- the committed edit is still caught" {
  REPO="$BATS_TEST_TMPDIR/repo8"
  mk_repo "$REPO"
  printf 'tampered\n' >> "$REPO/lib/gates.py"
  git -C "$REPO" commit -qam tamper
  MERGE_BASE="$(git -C "$REPO" merge-base main feature)"

  run env -C "$REPO" bash "$SCRIPT" --base "$MERGE_BASE"
  [ "$status" -eq 2 ]
  [[ "$output" == *"lib/gates.py"* ]]
}

@test "an EXPLICIT base ref that will not resolve HARD-FAILS, never a silent scope reduction" {
  REPO="$BATS_TEST_TMPDIR/repo9"
  mk_repo "$REPO"

  run env -C "$REPO" bash "$SCRIPT" --base no/such/ref
  [ "$status" -ne 0 ]
  [[ "$output" == *"no/such/ref"* ]]
  [[ "$output" != *PASS* ]]

  # danger half: a real forbidden edit exists and the bogus ref must STILL
  # fail -- a degrading implementation falls back to worktree-only scope
  # here and would (wrongly) exit 0 since the edit is already committed.
  printf 'tampered\n' >> "$REPO/lib/gates.py"
  git -C "$REPO" commit -qam tamper
  run env -C "$REPO" bash "$SCRIPT" --base no/such/ref
  [ "$status" -ne 0 ]
}

@test "with NO --base and none derivable, a clean tree exits 0 and states the reduced scope" {
  REPO="$BATS_TEST_TMPDIR/repo10"
  mk_repo "$REPO"

  run env -C "$REPO" bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *PASS* ]]
  [[ "$output" == *"staged"* ]]
  [[ "$output" == *"working"* ]]
  [[ "$output" != *"committed"* ]]
}

@test "in that same no-base-argument repo, a STAGED forbidden edit is STILL caught" {
  REPO="$BATS_TEST_TMPDIR/repo11"
  mk_repo "$REPO"
  printf 'tampered\n' >> "$REPO/lib/gates.py"
  git -C "$REPO" add "$REPO/lib/gates.py"

  run env -C "$REPO" bash "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"lib/gates.py"* ]]
}

@test "an unknown argument exits 2, and --base with no value exits 2" {
  REPO="$BATS_TEST_TMPDIR/repo12"
  mk_repo "$REPO"

  run env -C "$REPO" bash "$SCRIPT" --bogus
  [ "$status" -eq 2 ]

  run env -C "$REPO" bash "$SCRIPT" --base
  [ "$status" -eq 2 ]
}

@test "run outside any git repository, it exits 2 with a clear reason" {
  OUTSIDE="$BATS_TEST_TMPDIR/not-a-repo"
  mkdir -p "$OUTSIDE"

  run env -C "$OUTSIDE" bash "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"git repository"* ]]
}
