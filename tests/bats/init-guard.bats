#!/usr/bin/env bats
# init-guard.sh contract: advisory (always exit 0) by default, --strict exits 1,
# silent when all three markers are present, and registry presence follows
# HEAD-tracked semantics (an uncommitted registry still warns).

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/scripts/gsd/init-guard.sh"
  SCRATCH="$BATS_TEST_TMPDIR/scratch"
  mkdir -p "$SCRATCH"
  git -C "$SCRATCH" init -q
  git -C "$SCRATCH" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
}

@test "fully-initialized repo: silent, exit 0" {
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" != *"INIT-GUARD:"* ]]
}

@test "bare repo warns about registry but still exits 0" {
  cd "$SCRATCH"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ENV-REGISTRY-ABSENT"* ]]
  [[ "$output" == *"/ffs-init"* ]]
}

@test "bare repo with --strict exits 1" {
  cd "$SCRATCH"
  run bash "$SCRIPT" --strict
  [ "$status" -eq 1 ]
  [[ "$output" == *"INIT-GUARD:"* ]]
}

@test "uncommitted registry still counts as absent (HEAD semantics)" {
  cd "$SCRATCH"
  mkdir -p config
  printf '# schema: ffs.environments/v1\nenvironments: []\n' > config/environments.yaml
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ENV-REGISTRY-ABSENT"* ]]
}

@test "committed registry clears the registry warning" {
  cd "$SCRATCH"
  mkdir -p config
  printf '# schema: ffs.environments/v1\nenvironments: []\n' > config/environments.yaml
  git add config/environments.yaml
  git -c user.email=t@t -c user.name=t commit -qm registry
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" != *"ENV-REGISTRY-ABSENT"* ]]
}
