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

# A complete, bound record plus its matching authoritative ledger row.  Tests
# deliberately alter one fact after this helper so a refusal cannot be caused
# by an accidentally malformed fixture.
write_live_takeover_fixture() {
  local now="${1:-1700000000}"
  local record="$(dirname "$STORE")/takeover/spec-006.json"
  local canonical
  canonical="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$STORE")"
  mkdir -p "$(dirname "$record")"
  python3 - "$STORE" "$record" "$canonical" "$now" "$(git rev-parse HEAD)" <<'PY'
import hashlib,json,sys
store,record,canonical,created,head=sys.argv[1:]
state={"_autonomy":{"spec-006":{"takeover_expected":True,
       "takeover_created_at":int(created),
       "takeover_dirty_digest":hashlib.sha256(b"").hexdigest(),
       "preflight":{"pass":True,"checked_at":int(created)},
       "grants":{"ship:gsd":{"granted_at":int(created),"expires_at":4102444800}}}},
       "_findings":[]}
with open(store,"w") as f: json.dump(state,f)
record_data={"schema_version":1,"created_at":int(created),
 "ids":{"spec_id":"006","run_id":"spec-006"},"gates_store":canonical,
 "gates_store_anchor":hashlib.sha256(canonical.encode()).hexdigest(),
 "git_state":{"branch":"006-takeover","head":head,"upstream":"","dirty":[]},
 "preflight":{"pass":True},"grants":[{"action":"ship:gsd","granted_at":int(created),"expires_at":4102444800}],
 "pendings":[],"promotions":[],"runner":{},"unresolved_findings":[],"phases":[],"evidence":[],
 "forbid":[],"resume":{"command":"/spec-status 006","preconditions":[]}}
with open(record,"w") as f: json.dump(record_data,f)
PY
}

assert_single_refusal() {
  local reason="$1"
  [ "$status" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -c "^TAKEOVER-REFUSED:${reason}$")" -eq 1 ] || { echo "$output" >&3; return 1; }
  [ "$(printf '%s\n' "$output" | grep -c '^Unblock (operator): ')" -eq 1 ]
}

seed_live_authority() {
  local now="$(date +%s)"
  python3 - "$STORE" "$now" <<'PY'
import json,os,sys
p,now=sys.argv[1],int(sys.argv[2])
d=json.load(open(p)) if os.path.exists(p) else {}
auto=d.setdefault('_autonomy',{}).setdefault('spec-006',{})
auto['preflight']={'pass':True,'checked_at':now}
auto['grants']={'ship:gsd':{'granted_at':now,'expires_at':now+86400}}
json.dump(d,open(p,'w'))
PY
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
  seed_live_authority
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
  local store_dir="$(dirname "$STORE")"
  mkdir -p "$store_dir/takeover"
  printf sentinel > "$BATS_TEST_TMPDIR/target"
  ln -s "$BATS_TEST_TMPDIR/target" "$store_dir/takeover/spec-006.json"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -ne 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/target")" = sentinel ]
}

@test "wall JSON output shares the successful verdict" {
  write_live_takeover_fixture "$(date +%s)"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006 --json
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
  seed_live_authority
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  local store_dir
  store_dir="$(env GATES_STORE="$STORE" python3 "$GATES" store-dir)"
  printf '{"pid":2147483647,"pid_start_time":"0","boot_session_id":"unknown","claimed_at":1,"run_id":"spec-006"}\n' > "$store_dir/.takeover-check.lock"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
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

@test "writer JSON symlink preserves Markdown sibling and external target" {
  local store_dir="$(dirname "$STORE")"
  mkdir -p "$store_dir/takeover"
  printf old-markdown > "$store_dir/takeover/spec-006.md"
  printf external > "$BATS_TEST_TMPDIR/external-json"
  ln -sf "$BATS_TEST_TMPDIR/external-json" "$store_dir/takeover/spec-006.json"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -ne 0 ]
  [ "$(cat "$store_dir/takeover/spec-006.md")" = old-markdown ]
  [ "$(cat "$BATS_TEST_TMPDIR/external-json")" = external ]
}

@test "writer fault retains expectation and stale Markdown is generation-detectable" {
  local store_dir="$(dirname "$STORE")"
  mkdir -p "$store_dir/takeover"
  printf '# Takeover record\n\ngeneration: old\n' > "$store_dir/takeover/spec-006.md"
  run env TAKEOVER_FAULT_AFTER_JSON=1 GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -ne 0 ]
  [ -f "$store_dir/takeover/spec-006.json" ]
  [ "$(cat "$store_dir/takeover/spec-006.md")" = $'# Takeover record\n\ngeneration: old' ]
  run python3 - "$STORE" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['_autonomy']['spec-006']['takeover_expected'] is True
PY
  [ "$status" -eq 0 ]
}

@test "writer produces regular 0600 artifact siblings" {
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  local store_dir="$(dirname "$STORE")/takeover"
  [ "$(stat -f '%Lp' "$store_dir/spec-006.json")" = 600 ]
  [ "$(stat -f '%Lp' "$store_dir/spec-006.md")" = 600 ]
  [ -z "$(find "$store_dir" -name '.spec-006.*.tmp' -print -quit)" ]
}

# Plan 01-04 RED: a record is evidence, never the selector for the live
# check floor. These use a deterministic clock seam added by the GREEN change.
@test "refusal floor checks fresh preflight and required ship grant despite empty record fields" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  python3 - "$STORE" "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
p=sys.argv[2]; store=json.load(open(sys.argv[1])); store['_autonomy']['spec-006']['preflight']={}
json.dump(store,open(sys.argv[1],'w'))
d=json.load(open(p)); d['preflight']={}; d['grants']=[]
json.dump(d,open(p,'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal preflight-stale
  [[ "$output" == *$'Unblock (operator): /preflight'* ]]
}

@test "record mismatch is rejected before live authority when binding metadata disagrees" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d['created_at'] += 1; json.dump(d,open(p,'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
}

@test "inclusive 72-hour ledger age refuses while one second under reaches later checks" {
  local now="$(date +%s)"
  local created="$((now - 259200))"
  write_live_takeover_fixture "$created"
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$now" bash "$WALL" --run-id spec-006
  assert_single_refusal grant-expired
  [[ "$output" == *'Unblock (operator): /spec-status'* ]]

  write_live_takeover_fixture "$((now - 259199))"
  python3 - "$STORE" "$now" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d['_autonomy']['spec-006']['preflight']['checked_at']=int(sys.argv[2]); json.dump(d,open(p,'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$now" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
}

@test "exact dirty baseline rejects an added untracked entry" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  printf 'new\n' > added-untracked.txt
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal dirty-worktree
}

@test "hostile NUL-delimited filename remains one dirty entry" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  local hostile=$'tab\tquote"\nnewline'
  printf 'hostile\n' > "$hostile"
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal dirty-worktree
}

@test "live grant clock divergence refuses record-mismatch" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  python3 - "$STORE" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d['_autonomy']['spec-006']['grants']['ship:gsd']['granted_at'] += 1
json.dump(d,open(p,'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
}

# Plan 01-05 RED: the wall must use held descriptors rather than reopening
# hostile names.  These contracts intentionally name the new transaction
# boundary; they fail until the fd bootstrap exists.
@test "fd transaction rejects a symlinked takeover directory without following it" {
  mkdir -p "$BATS_TEST_TMPDIR/outside"
  mkdir -p "$(dirname "$STORE")"
  printf '{}' > "$STORE"
  ln -s "$BATS_TEST_TMPDIR/outside" "$(dirname "$STORE")/takeover"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 1 ]
  assert_single_refusal record-mismatch
}

@test "fd transaction keeps absent expectation lookup pinned to its original evidence fd" {
  local now="$(date +%s)"
  mkdir -p "$(dirname "$STORE")"
  python3 - "$STORE" "$now" <<'PY'
import json,sys
json.dump({'_autonomy':{'spec-006':{'takeover_expected':True}}},open(sys.argv[1],'w'))
PY
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  assert_single_refusal missing-record
}

@test "fd transaction consumes only the originally opened regular record" {
  write_live_takeover_fixture "$(date +%s)"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
  [ -z "$(find "$(dirname "$STORE")/takeover" -name 'spec-006.json' -print -quit)" ]
  [ -n "$(find "$(dirname "$STORE")/takeover" -name 'spec-006.consumed.*.json' -print -quit)" ]
}
