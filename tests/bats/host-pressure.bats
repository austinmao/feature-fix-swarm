#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  HP="$ROOT/scripts/gsd/host-pressure.sh"
}

@test "fixture ok -> rc 0, fixture high -> rc 75, both typed" {
  run env FFS_HOST_PRESSURE_FIXTURE=ok bash "$HP" check
  [ "$status" -eq 0 ]; [[ "$output" == 'HOST-PRESSURE:ok swap_pct=0 free_pct=50' ]]
  run env FFS_HOST_PRESSURE_FIXTURE=high bash "$HP" check
  [ "$status" -eq 75 ]; [[ "$output" == 'HOST-PRESSURE:high swap_pct=95 free_pct=3' ]]
}

@test "live reading is typed ok/high/unknown and never crashes" {
  run bash "$HP" check
  [ "$status" -eq 0 ] || [ "$status" -eq 75 ]
  [[ "$output" == HOST-PRESSURE:ok* ]] || [[ "$output" == HOST-PRESSURE:high* ]] || [[ "$output" == HOST-PRESSURE:unknown ]]
}

@test "kill switch and invalid fixture/env are typed" {
  run env FFS_HOST_PRESSURE=off FFS_HOST_PRESSURE_FIXTURE=high bash "$HP" check
  [ "$status" -eq 0 ]; [ "$output" = HOST-PRESSURE:disabled ]
  run env FFS_HOST_PRESSURE_FIXTURE=bogus bash "$HP" check
  [ "$status" -eq 2 ]
  run env FFS_PRESSURE_SWAP_PCT=abc bash "$HP" check
  [ "$status" -eq 2 ]; [[ "$output" == *invalid-env* ]]
}

@test "wait under high pressure backs off, caps at --max-secs, then launches anyway with rc 0" {
  run env FFS_HOST_PRESSURE_FIXTURE=high FFS_PRESSURE_BACKOFF_BASE_SECS=1 bash "$HP" wait --max-secs 1
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -c 'HOST-PRESSURE:high')" -eq 2 ]
  [[ "$output" == *'HOST-PRESSURE:waiting sleep=1s'* ]]
  [[ "$output" == *'HOST-PRESSURE:wait-exhausted'* ]]
}

@test "wait under ok pressure returns immediately" {
  run env FFS_HOST_PRESSURE_FIXTURE=ok bash "$HP" wait
  [ "$status" -eq 0 ]; [[ "$output" == HOST-PRESSURE:ok* ]]
}

@test "vm.swapusage line parses to a used percentage" {
  run bash -c 'source <(sed -n "/^parse_swapusage()/,/^}/p" "$1"); echo "total = 17408.00M  used = 16312.94M  free = 1095.06M  (encrypted)" | parse_swapusage' _ "$HP"
  [ "$status" -eq 0 ]; [ "$output" = 93 ]
}
