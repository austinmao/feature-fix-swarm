#!/usr/bin/env bats

# Phase 1 takeover contracts.  These intentionally exercise the real
# collector/wall boundary with a private evidence store and local git only.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  COLLECTOR="$ROOT/skills/spec-status/scripts/collect-status-facts.sh"
  WALL="$ROOT/scripts/gsd/takeover-check.sh"
  GATES="$ROOT/lib/gates.py"
  REPO="$BATS_TEST_TMPDIR/repo"
  STORE="$BATS_TEST_TMPDIR/evidence.json"
  mkdir -p "$REPO/specs/006-autonomous-landing"
  cd "$REPO" || return 1
  git init -q
  git config user.email t@example.test
  git config user.name takeover-test
  touch README.md specs/006-autonomous-landing/spec.md
  git add README.md specs
  git commit -qm init
  git checkout -qb 006-takeover
}

@test "store-dir and store-path resolve without loading the ledger" {
  mkdir -p "$BATS_TEST_TMPDIR/store"
  local resolved_store
  resolved_store="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$BATS_TEST_TMPDIR/store")"
  run env GATES_STORE="$BATS_TEST_TMPDIR/store/evidence.json" python3 "$GATES" store-dir
  [ "$status" -eq 0 ]
  [ "$output" = "$resolved_store" ]
  run env GATES_STORE="$BATS_TEST_TMPDIR/store/evidence.json" python3 "$GATES" store-path
  [ "$status" -eq 0 ]
  [ "$output" = "$resolved_store/evidence.json" ]
}

@test "PATH-001 collector creates a record accepted by the wall" {
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  [ -f "$(dirname "$STORE")/takeover/spec-006.json" ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = "TAKEOVER-OK" ]
}

@test "absence split is explicit and expectation makes missing record refuse" {
  run env -u GATES_STORE bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = "TAKEOVER-NONE" ]
  run env -u GATES_STORE python3 "$GATES" takeover-expect spec-006
  [ "$status" -eq 0 ]
  run env -u GATES_STORE bash "$WALL" --run-id spec-006
  [ "$status" -ne 0 ]
  [[ "$output" == *"TAKEOVER-REFUSED:missing-record"* ]]
}

@test "writer refuses a planted output symlink without changing its target" {
  mkdir -p "$REPO/.feature-fix-swarm/takeover"
  printf sentinel > "$BATS_TEST_TMPDIR/target"
  ln -s "$BATS_TEST_TMPDIR/target" "$REPO/.feature-fix-swarm/takeover/spec-006.json"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -ne 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/target")" = sentinel ]
}

@test "wall JSON output shares the successful verdict" {
  local canonical
  canonical="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$REPO/.feature-fix-swarm/evidence.json")"
  mkdir -p "$REPO/.feature-fix-swarm/takeover"
  cat > "$REPO/.feature-fix-swarm/takeover/spec-006.json" <<EOF
{"schema_version":1,"created_at":$(date +%s),"ids":{"spec_id":"006","run_id":"spec-006"},"gates_store":"$canonical","gates_store_anchor":"$(printf %s "$canonical" | shasum -a 256 | awk '{print $1}')","git_state":{"branch":"006-takeover","head":"$(git rev-parse HEAD)","upstream":""},"preflight":{},"grants":[],"pendings":[],"promotions":[],"runner":{},"unresolved_findings":[],"phases":[],"evidence":[],"forbid":[],"resume":{"command":"","preconditions":[]}}
EOF
  run env -u GATES_STORE bash "$WALL" --run-id spec-006 --json
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"TAKEOVER-OK"'* ]]
}

@test "record schema keeps typed empty collections and deterministic forbid boundary" {
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  run python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json, sys
d=json.load(open(sys.argv[1]))
for key in ("grants", "pendings", "promotions", "unresolved_findings", "phases", "evidence", "forbid"):
    assert isinstance(d[key], list), key
assert d["forbid"] == []
assert isinstance(d["resume"]["preconditions"], list)
PY
  [ "$status" -eq 0 ]
}

@test "list is deterministic display-only metadata" {
  mkdir -p "$REPO/.feature-fix-swarm/takeover"
  for id in spec-007 spec-006; do
    cat > "$REPO/.feature-fix-swarm/takeover/$id.json" <<EOF
{"created_at":1,"ids":{"run_id":"$id"},"git_state":{"branch":"b"},"resume":{"command":"echo \u001b[31minert"}}
EOF
  done
  run env -u GATES_STORE bash "$WALL" --list
  [ "$status" -eq 0 ]
  [[ "$output" == spec-006$'\t'* ]]
  [[ "$output" == *$'\nspec-007\t'* ]]
}

@test "spec-status documents the 1.2 takeover writer contract" {
  grep -q 'version: "1.2.0"' "$ROOT/skills/spec-status/SKILL.md"
  grep -q 'deterministic forbid' "$ROOT/skills/spec-status/SKILL.md"
}

# Phase 01-02 RED contract: a refusal is an operator-facing, closed grammar
# verdict.  This deliberately uses a malformed bound store so no ledger read
# is needed to prove the rendering contract.
@test "refuses decoy-store with exactly one safe operator remedy" {
  mkdir -p "$REPO/.feature-fix-swarm/takeover"
  cat > "$REPO/.feature-fix-swarm/takeover/spec-006.json" <<'EOF'
{"schema_version":1,"created_at":1,"ids":{"spec_id":"006","run_id":"spec-006"},"gates_store":"/decoy/evidence.json","gates_store_anchor":"bad","git_state":{"branch":"006-takeover","head":"x","upstream":""},"preflight":{},"grants":[],"pendings":[],"promotions":[],"runner":{},"unresolved_findings":[],"phases":[],"evidence":[],"forbid":[],"resume":{"command":"","preconditions":[]}}
EOF
  run env -u GATES_STORE bash "$WALL" --run-id spec-006
  [ "$status" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -c '^TAKEOVER-REFUSED:decoy-store$')" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -c '^Unblock (operator): /spec-status$')" -eq 1 ]
}

@test "rejects --rearm as usage without mutating the record" {
  mkdir -p "$REPO/.feature-fix-swarm/takeover"
  printf '{"ids":{"run_id":"spec-006"}}\n' > "$REPO/.feature-fix-swarm/takeover/spec-006.json"
  run env -u GATES_STORE bash "$WALL" --run-id spec-006 --rearm
  [ "$status" -eq 2 ]
  [ -f "$REPO/.feature-fix-swarm/takeover/spec-006.json" ]
}

@test "reclaims a same-boot dead-owner takeover lock before checking" {
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  local store_dir
  store_dir="$(env -u GATES_STORE python3 "$GATES" store-dir)"
  printf '{"pid":2147483647,"pid_start_time":"0","boot_session_id":"unknown","claimed_at":1,"run_id":"spec-006"}\n' > "$store_dir/.takeover-check.lock"
  run env -u GATES_STORE bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = "TAKEOVER-OK" ]
  [ ! -e "$store_dir/.takeover-check.lock" ]
}

# Plan 01-03 RED contracts.  Keep these on the real collector/writer path: an
# override is an authority selection, never a fixture-only alternate path.
@test "live record facts are complete and dirty baseline is normalized" {
  mkdir -p "$REPO/.planning/run-state" "$REPO/.planning/phases/01-demo"
  printf 'stopped\n' > "$REPO/.planning/run-state/gsd-run.status"
  printf '999999\n' > "$REPO/.planning/run-state/gsd-run.pid"
  touch "$REPO/.planning/phases/01-demo/01-01-PLAN.md"
  touch "$REPO/.planning/phases/01-demo/01-01-SUMMARY.md"
  printf 'uncommitted\n' > dirty.txt
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  local record
  record="$(dirname "$STORE")/takeover/spec-006.json"
  [ -f "$record" ]
  run python3 - "$record" <<'PY'
import hashlib,json,sys
d=json.load(open(sys.argv[1]))
assert d['runner']['status'] == 'stopped'
assert d['runner']['pid'] == 999999
assert d['phases'] and d['phases'][0]['plan'] == '.planning/phases/01-demo/01-01-PLAN.md'
assert d['git_state']['dirty'] == sorted(d['git_state']['dirty']) and d['git_state']['dirty']
assert d['resume']['command'].startswith('/spec-status 006')
PY
  [ "$status" -eq 0 ]
  run python3 - "$STORE" <<'PY'
import hashlib,json,sys
d=json.load(open(sys.argv[1])); row=d['_autonomy']['spec-006']
assert row['takeover_expected'] is True
assert row['takeover_created_at'] > 0
assert len(row['takeover_dirty_digest']) == 64
PY
  [ "$status" -eq 0 ]
}

@test "collector override keeps record markdown and expectation in one store" {
  local default_store="$REPO/.feature-fix-swarm/evidence.json"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  [ -f "$(dirname "$STORE")/takeover/spec-006.json" ]
  [ -f "$(dirname "$STORE")/takeover/spec-006.md" ]
  [ ! -e "$REPO/.feature-fix-swarm/takeover/spec-006.json" ]
  run python3 - "$STORE" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['_autonomy']['spec-006']['takeover_expected']
PY
  [ "$status" -eq 0 ]
  [ ! -e "$default_store" ]
}

@test "deterministic forbid ignores prose and reports named mid-rebase probe" {
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  run python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['forbid'] == []
PY
  [ "$status" -eq 0 ]
  mkdir -p "$REPO/.git/rebase-merge"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  run python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
rows=json.load(open(sys.argv[1]))['forbid']
assert rows == [{'action':'mid-rebase','probe':'mid-rebase','reason':'git rebase is in progress'}]
PY
  [ "$status" -eq 0 ]
}

@test "writer pair symlink preserves regular sibling and external target" {
  local store_dir="$(dirname "$STORE")"
  mkdir -p "$store_dir/takeover"
  printf old-json > "$store_dir/takeover/spec-006.json"
  printf external > "$BATS_TEST_TMPDIR/external"
  ln -sf "$BATS_TEST_TMPDIR/external" "$store_dir/takeover/spec-006.md"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -ne 0 ]
  [ "$(cat "$store_dir/takeover/spec-006.json")" = old-json ]
  [ "$(cat "$BATS_TEST_TMPDIR/external")" = external ]
}
