#!/usr/bin/env bats
# spec-status collector RUNS section: runs for the spec (drive state matched by
# "spec"), launches per run, review meters, and exhausted-record count.

setup() {
  PACKAGE_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  COLLECTOR="$PACKAGE_ROOT/skills/spec-status/scripts/collect-status-facts.sh"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/specs/123-target" "$REPO/.planning/run-state"
  cd "$REPO" || return 1
  git init -q -b main
  git config user.email test@example.com
  git config user.name "Spec Status Test"
  printf 'x\n' > specs/123-target/spec.md
  git add specs && git commit -q -m init

  jq -n '{run_id:"d1",spec:"spec-123",state:"done",steps:[{phase:"1",launches:2},{phase:"2",launches:1}]}' > .planning/run-state/drive-d1.json
  jq -n '{run_id:"d2",spec:"spec-123",state:"parked",steps:[{phase:"1",launches:4}]}' > .planning/run-state/drive-d2.json
  jq -n '{run_id:"other",spec:"spec-456",state:"done",steps:[{phase:"1",launches:9}]}' > .planning/run-state/drive-other.json

  export GATES_STORE="$BATS_TEST_TMPDIR/store.json"
  jq -n '{_loops:{"spec-123":{"review:spec#cap":{max:2,used:2},"review:wall:01-foo#cap":{max:2,used:1}}},
          _exhausted:{"spec-123":[{loop:"review:spec",at:"t",text:"x"}]}}' > "$GATES_STORE"
  export GSD_RUN_ID=spec-123
}

@test "collector emits a RUNS section with run count, meters and exhausted count for the spec" {
  run bash "$COLLECTOR" 123
  [ "$status" -eq 0 ]
  [[ "$output" == *"RUNS:"* ]]
  runs="$(printf '%s\n' "$output" | sed -n '/^RUNS:/,/^==/p')"
  [[ "$runs" == *"runs 2"* || "$runs" == *"runs: 2"* || "$runs" == *"run count 2"* ]]
  [[ "$runs" == *"review:spec 2/2"* ]]
  [[ "$runs" == *"review:wall:01-foo 1/2"* ]]
  [[ "$runs" == *"exhausted 1"* ]]
  # launches per run, and the other spec's drive is excluded
  [[ "$runs" == *"d1"* && "$runs" == *"launches 3"* ]]
  [[ "$runs" == *"d2"* && "$runs" == *"launches 4"* ]]
  [[ "$runs" != *"other"* ]]
}
