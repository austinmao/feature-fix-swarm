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

@test "validate refuses unknown states, wake types, corrupt JSON and unsafe resumes" {
  record="$REPO/.planning/run-state/lifecycle-run-2.json"
  printf '{"run_id":"run-2","state":"unknown"}' > "$record"
  run bash "$LIFECYCLE" validate run-2
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]
  printf '{not-json' > "$record"
  run bash "$LIFECYCLE" validate run-2
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]; [[ "$output" != *Traceback* ]]
  cp /dev/null "$record"
  jq -n '{run_id:"run-2",state:"running",reason:"x",wake_condition:{type:"bad",params:{}},resume_argv:["scripts/gsd/plan-wall.sh"],budgets:{respawns:1,wakes:1,ci_reruns:1},waiting_since:null,wake_at:null,updated_at:"now"}' > "$record"
  run bash "$LIFECYCLE" validate run-2
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]
  jq '.wake_condition.type="manual" | .resume_argv=["/bin/echo"]' "$record" > "$record.tmp" && mv "$record.tmp" "$record"
  run bash "$LIFECYCLE" validate run-2
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]
}

@test "terminal transitions and checkpoint resurrection are refused without mutation" {
  bash "$LIFECYCLE" checkpoint terminal running start manual '{}' '["scripts/gsd/plan-wall.sh"]' '{"respawns":2}'
  bash "$LIFECYCLE" transition terminal quarantined bad >/dev/null
  record="$REPO/.planning/run-state/lifecycle-terminal.json"; before="$(cat "$record")"
  run bash "$LIFECYCLE" transition terminal runnable no
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]; [ "$(cat "$record")" = "$before" ]
  run bash "$LIFECYCLE" checkpoint terminal waiting no manual '{}' '["scripts/gsd/plan-wall.sh"]' '{}'
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]; [ "$(cat "$record")" = "$before" ]
  run bash "$LIFECYCLE" transition terminal waiting still-no
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]
}

@test "budgets only decrement across resumed checkpoints and concurrent decrements" {
  bash "$LIFECYCLE" checkpoint budget running start manual '{}' '["scripts/gsd/plan-wall.sh"]' '{"respawns":2}'
  bash "$LIFECYCLE" decrement budget respawns
  run bash "$LIFECYCLE" checkpoint budget waiting wait manual '{}' '["scripts/gsd/plan-wall.sh"]' '{"respawns":1}'
  [ "$status" -eq 0 ]
  run jq -r '.budgets.respawns' "$REPO/.planning/run-state/lifecycle-budget.json"
  [ "$output" = 1 ]
  run bash "$LIFECYCLE" checkpoint budget runnable raise manual '{}' '["scripts/gsd/plan-wall.sh"]' '{"respawns":2}'
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]
  bash "$LIFECYCLE" checkpoint race running start manual '{}' '["scripts/gsd/plan-wall.sh"]' '{"respawns":2}'
  bash "$LIFECYCLE" decrement race respawns & first=$!
  bash "$LIFECYCLE" decrement race respawns & second=$!
  wait "$first"; wait "$second"
  run jq -r '.budgets.respawns' "$REPO/.planning/run-state/lifecycle-race.json"
  [ "$output" = 0 ]
}

@test "a legal transition remains possible after a rejected transition and concurrent writes stay JSON" {
  bash "$LIFECYCLE" checkpoint legal running start manual '{}' '["scripts/gsd/plan-wall.sh"]' '{}'
  run bash "$LIFECYCLE" transition legal runnable wrong
  [ "$status" -eq 1 ]; [[ "$output" == LIFECYCLE:* ]]
  run bash "$LIFECYCLE" transition legal waiting right
  [ "$status" -eq 0 ]
  bash "$LIFECYCLE" transition legal runnable writer-one & first=$!
  wait "$first"
  bash "$LIFECYCLE" transition legal running writer-two & second=$!
  wait "$second"
  run jq -e . "$REPO/.planning/run-state/lifecycle-legal.json"
  [ "$status" -eq 0 ]
}

@test "write verbs reject a forged resume argv without changing the record" {
  bash "$LIFECYCLE" checkpoint guarded running start manual '{}' '["scripts/gsd/plan-wall.sh"]' '{"respawns":1}'
  record="$REPO/.planning/run-state/lifecycle-guarded.json"
  jq '.resume_argv=["/bin/sh","-c","bad"]' "$record" > "$record.tmp" && mv "$record.tmp" "$record"
  before="$(cat "$record")"
  run bash "$LIFECYCLE" decrement guarded respawns
  [ "$status" -eq 1 ]
  [ "$(cat "$record")" = "$before" ]
}
