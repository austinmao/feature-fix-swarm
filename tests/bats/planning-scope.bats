#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/planning-scope.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/.planning"
  git -C "$REPO" init -q
  unset GSD_PROJECT
}

roadmap() { # $1 = planning dir relative to the repo
  mkdir -p "$REPO/$1"
  printf '%s\n' '# Roadmap' > "$REPO/$1/ROADMAP.md"
}

scope() { # $1 = spec id, $2 = GSD_PROJECT value ('' = unset)
  if [ -n "${2:-}" ]; then
    run bash -c "cd $(printf '%q' "$REPO") && GSD_PROJECT=$(printf '%q' "$2") bash $(printf '%q' "$SCRIPT") $(printf '%q' "$1")"
  else
    run bash -c "cd $(printf '%q' "$REPO") && bash $(printf '%q' "$SCRIPT") $(printf '%q' "$1")"
  fi
}

@test "a spec with its own project wins over the root project (derived)" {
  roadmap .planning
  roadmap .planning/431

  scope 431

  [ "$status" -eq 0 ]
  [ "$output" = "431 .planning/431 derived" ]
}

@test "a slugged spec id resolves its numeric project" {
  roadmap .planning/431

  scope 431-courses-learn

  [ "$status" -eq 0 ]
  [ "$output" = "431 .planning/431 derived" ]
}

@test "an exact slug project is preferred over the numeric one" {
  roadmap .planning/431
  roadmap .planning/431-courses-learn

  scope 431-courses-learn

  [ "$status" -eq 0 ]
  [ "$output" = "431-courses-learn .planning/431-courses-learn derived" ]
}

@test "no per-spec project falls back to the root project" {
  roadmap .planning

  scope 432

  [ "$status" -eq 0 ]
  [ "$output" = "- .planning root" ]
}

@test "a matching GSD_PROJECT in the env is session mode" {
  roadmap .planning
  roadmap .planning/431

  scope 431 431

  [ "$status" -eq 0 ]
  [ "$output" = "431 .planning/431 session" ]
}

@test "a GSD_PROJECT that contradicts the spec's own project is refused" {
  roadmap .planning/431
  roadmap .planning/432

  scope 431 432

  [ "$status" -eq 2 ]
  [[ "$output" == *"GSD_PROJECT=432 but spec 431 plans under .planning/431/"* ]]
}

@test "a GSD_PROJECT without a ROADMAP is refused, never treated as root" {
  roadmap .planning

  scope 431 999

  [ "$status" -eq 2 ]
  [[ "$output" == *"GSD_PROJECT=999 but .planning/999/ROADMAP.md is missing"* ]]
}

@test "a GSD_PROJECT naming another valid project is honored when the spec has none" {
  roadmap .planning/courses

  scope 433 courses

  [ "$status" -eq 0 ]
  [ "$output" = "courses .planning/courses session" ]
}

@test "unsafe values are refused" {
  roadmap .planning/431

  scope 431 ../x
  [ "$status" -eq 2 ]
  [[ "$output" == *"GSD_PROJECT must be one path segment"* ]]

  scope '../431'
  [ "$status" -eq 2 ]
  [[ "$output" == *"spec id must be NNN or NNN-slug"* ]]

  run bash "$SCRIPT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: planning-scope.sh <spec-id>"* ]]
}
