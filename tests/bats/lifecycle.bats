#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  LIFECYCLE="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/lifecycle.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/.planning/run-state" "$REPO/scripts/gsd"
  cd "$REPO"
  git init -q -b main
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
}

@test "checkpoint transition show is a durable atomic round trip" {
  run bash "$LIFECYCLE" checkpoint run-1 running start wall-decided '{}' '["scripts/gsd/plan-wall.sh"]' '{"respawns":1,"wakes":2,"ci_reruns":3}'
  [ "$status" -eq 0 ]
  record="$REPO/.planning/run-state/lifecycle-run-1.json"
  [ -f "$record" ]
  run jq -e '.run_id == "run-1" and .state == "running" and (.wake_condition | has("type") and has("params")) and (.budgets | has("respawns") and has("wakes") and has("ci_reruns")) and has("waiting_since") and has("wake_at") and has("updated_at")' "$record"
  [ "$status" -eq 0 ]
  run bash "$LIFECYCLE" validate run-1
  [ "$status" -eq 0 ]
  run bash "$LIFECYCLE" transition run-1 waiting wall-pending
  [ "$status" -eq 0 ]
  [ "$output" = "LIFECYCLE:running>waiting run=run-1 reason=wall-pending" ]
  run bash "$LIFECYCLE" show run-1
  [ "$status" -eq 0 ]
  [[ "$output" == *'"state": "waiting"'* ]]
  [ -z "$(find "$REPO/.planning/run-state" -name '*.tmp' -print -quit)" ]
}
