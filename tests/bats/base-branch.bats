#!/usr/bin/env bats
# base-branch.sh — one resolver for the repo's default branch, so levers stop
# forking off a hardcoded `main` on `master` repos (borrowed: buildomator
# gsd-tools base-branch). Offline-only chain:
#   GSD_BASE_BRANCH env -> origin/HEAD symref -> local main -> local master -> main

LEVER="base-branch.sh"

setup() {
  SCRIPTS="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
  cd "$REPO"
  git init -q -b trunk
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
}

@test "env override wins over everything" {
  run env GSD_BASE_BRANCH=release bash "$SCRIPTS/$LEVER"
  [ "$status" -eq 0 ]
  [ "$output" = "release" ]
}

@test "origin/HEAD symref resolves when present" {
  git update-ref refs/remotes/origin/trunk HEAD
  git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/trunk
  run bash "$SCRIPTS/$LEVER"
  [ "$status" -eq 0 ]
  [ "$output" = "trunk" ]
}

@test "no origin: falls to existing local master over the main fallback" {
  git branch -m master
  run bash "$SCRIPTS/$LEVER"
  [ "$status" -eq 0 ]
  [ "$output" = "master" ]
}

@test "no origin, no main/master: falls back to main" {
  run bash "$SCRIPTS/$LEVER"
  [ "$status" -eq 0 ]
  [ "$output" = "main" ]
}

@test "local main preferred when both main and master exist" {
  git branch -m main
  git branch master
  run bash "$SCRIPTS/$LEVER"
  [ "$status" -eq 0 ]
  [ "$output" = "main" ]
}
