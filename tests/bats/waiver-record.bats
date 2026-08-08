#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  FIXTURE="$BATS_TEST_TMPDIR/waiver-fixture"
  mkdir -p "$FIXTURE/lib" "$FIXTURE/scripts/gsd"
  cp "$ROOT/lib/gates.py" "$FIXTURE/lib/gates.py"
  cp "$ROOT/scripts/gsd/waiver-record.sh" "$FIXTURE/scripts/gsd/waiver-record.sh"
  chmod +x "$FIXTURE/scripts/gsd/waiver-record.sh"
  git -C "$FIXTURE" init -q
}

@test "WR-001: shared recorder writes unattributed row in fixture canonical store" {
  run env -u GSD_RUN_ID "$FIXTURE/scripts/gsd/waiver-record.sh" canary-gate CANARY_GATE=off
  [ "$status" -eq 0 ]
  run python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))["waivers"][0]; assert r["run_id"] == "unattributed"; assert r["gate"] == "canary-gate"' "$FIXTURE/.feature-fix-swarm/evidence.json"
  [ "$status" -eq 0 ]
}

@test "WR-002: concurrent recorders preserve distinct durable rows" {
  "$FIXTURE/scripts/gsd/waiver-record.sh" canary-gate CANARY_GATE=off &
  first=$!
  "$FIXTURE/scripts/gsd/waiver-record.sh" qa-coverage-adversary QA_COVERAGE=off &
  second=$!
  wait "$first"
  wait "$second"
  run python3 -c 'import json,sys; assert len(json.load(open(sys.argv[1]))["waivers"]) == 2' "$FIXTURE/.feature-fix-swarm/evidence.json"
  [ "$status" -eq 0 ]
}
