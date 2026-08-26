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

# WALL-RESIDUALS fbc53626: JSON is the sole authoritative sibling and its
# rename is the commit point.  A writer fault between siblings must leave
# expectation-without-record (refused by the wall), never a trusted record.
@test "writer fault between siblings leaves expectation without an authoritative record" {
  local store_dir="$(dirname "$STORE")"
  mkdir -p "$store_dir/takeover"
  printf '# Takeover record\n\ngeneration: old\n' > "$store_dir/takeover/spec-006.md"
  run env TAKEOVER_FAULT_BEFORE_JSON=1 GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -ne 0 ]
  [ ! -e "$store_dir/takeover/spec-006.json" ]
  grep -q '^generation: ' "$store_dir/takeover/spec-006.md"
  ! grep -q '^generation: old$' "$store_dir/takeover/spec-006.md"
  run python3 - "$STORE" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['_autonomy']['spec-006']['takeover_expected'] is True
PY
  [ "$status" -eq 0 ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  assert_single_refusal missing-record
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

# Plan 01-05 Task 2 RED: lock names are hostile too.  A FIFO must never turn
# contention handling into a blocking read of an attacker-controlled payload.
@test "FIFO takeover lock refuses in bounded time without reading the FIFO" {
  write_live_takeover_fixture "$(date +%s)"
  local store_dir="$(dirname "$STORE")"
  mkfifo "$store_dir/.takeover-check.lock"
  run timeout 2 env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 1 ]
  assert_single_refusal runner-live
}

# Plan 01-06 RED: the producer is only useful to a cold session when evidence
# and resumption facts are real, typed, and rooted in the selected spec.
@test "fresh collector records sorted evidence and typed resume preconditions" {
  mkdir -p "$REPO/specs/006-autonomous-landing/evidence/nested"
  printf '{"proof":true}\n' > "$REPO/specs/006-autonomous-landing/evidence/z-proof.json"
  printf 'note\n' > "$REPO/specs/006-autonomous-landing/evidence/nested/a-note.txt"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  run python3 - "$(dirname "$STORE")/takeover/spec-006.json" "$STORE" <<'PY'
import json,sys
record=json.load(open(sys.argv[1]))
assert record['evidence'] == [
    'specs/006-autonomous-landing/evidence/nested/a-note.txt',
    'specs/006-autonomous-landing/evidence/z-proof.json',
]
resume=record['resume']
assert isinstance(resume['command'], str) and resume['command'].startswith('/spec-status 006')
assert isinstance(resume['preconditions'], list) and resume['preconditions']
assert any(row.get('kind') == 'gates_store' for row in resume['preconditions'])
assert any(row.get('kind') == 'git_head' for row in resume['preconditions'])
PY
  [ "$status" -eq 0 ]
}

# Plan 01-06 Task 2 RED: staging is a full-write transaction.  Partial and
# zero-progress writes must never publish truncated bytes.
@test "short write seams complete the payload and zero progress is rejected" {
  local dir="$BATS_TEST_TMPDIR/stage-dir"
  mkdir -p "$dir"
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$dir" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]
dir_fd = os.open(work, os.O_RDONLY)
payload = b'{"payload":"' + b'x' * 96 + b'"}\n'
real_write = os.write
os.write = lambda fd, view: real_write(fd, bytes(view)[:7])
try:
    io_mod.replace_bytes(dir_fd, "spec-006.json", payload)
finally:
    os.write = real_write
with open(os.path.join(work, "spec-006.json"), "rb") as fh:
    data = fh.read()
assert data == payload, "short writes were not completed: %d of %d bytes" % (len(data), len(payload))
os.write = lambda fd, view: 0
try:
    try:
        io_mod.replace_bytes(dir_fd, "spec-007.json", b"{}\n")
    finally:
        os.write = real_write
except OSError:
    pass
else:
    raise AssertionError("zero-progress write did not raise")
assert not os.path.exists(os.path.join(work, "spec-007.json")), "zero-progress attempt published a file"
leftovers = [n for n in os.listdir(work) if n.endswith(".tmp")]
assert not leftovers, "stranded stages: %r" % leftovers
print("short-write-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *short-write-ok* ]]
}

# Plan 01-06 Task 2 RED: every staging fault removes exactly the attempt-owned
# stage and never the final sibling.  Per WALL-RESIDUALS b80300b4 a directory
# fsync failure AFTER the atomic rename is a typed FSYNC-FAIL while the
# completed rename stands; the no-mutation contract is scoped to pre-rename
# failures only.
@test "stage faults unlink only the attempt-owned stage and keep the final sibling" {
  local dir="$BATS_TEST_TMPDIR/stage-dir"
  mkdir -p "$dir"
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$dir" <<'PY'
import importlib.util, os, stat, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]
dir_fd = os.open(work, os.O_RDONLY)
name = "spec-006.json"
old = b'{"generation":"old"}\n'
io_mod.replace_bytes(dir_fd, name, old)

def final_bytes():
    with open(os.path.join(work, name), "rb") as fh:
        return fh.read()

def no_stage(label):
    leftovers = [n for n in os.listdir(work) if n.endswith(".tmp")]
    assert not leftovers, "%s left stranded stages: %r" % (label, leftovers)

real_write, real_fsync, real_replace = os.write, os.fsync, os.replace
real_validate = io_mod.validate_final

def expect_failure(label, exc=OSError):
    try:
        io_mod.replace_bytes(dir_fd, name, b'{"generation":"hostile"}\n')
    except exc:
        pass
    else:
        raise AssertionError("%s did not raise" % label)

def boom(*a, **k):
    raise OSError("injected fault")

os.write = boom
try:
    expect_failure("write fault")
finally:
    os.write = real_write
no_stage("write fault"); assert final_bytes() == old

os.fsync = boom
try:
    expect_failure("fsync fault")
finally:
    os.fsync = real_fsync
no_stage("fsync fault"); assert final_bytes() == old

calls = {"n": 0}
def late_validate(directory_fd, target):
    calls["n"] += 1
    if calls["n"] > 1:
        raise io_mod.UnsafeTakeoverPath("injected validation fault")
    return real_validate(directory_fd, target)
io_mod.validate_final = late_validate
try:
    expect_failure("validation fault", io_mod.UnsafeTakeoverPath)
finally:
    io_mod.validate_final = real_validate
no_stage("validation fault"); assert final_bytes() == old

stages = []
def failing_replace(src, dst, **kw):
    st = os.stat(src, dir_fd=kw.get("src_dir_fd"), follow_symlinks=False)
    assert stat.S_IMODE(st.st_mode) == 0o600, "stage is not 0600"
    stages.append(src)
    raise OSError("injected replace")
os.replace = failing_replace
try:
    expect_failure("replace fault one")
    expect_failure("replace fault two")
finally:
    os.replace = real_replace
no_stage("replace fault"); assert final_bytes() == old
assert len(set(stages)) == 2, "stage names are reused across attempts: %r" % stages

fsynced = {"n": 0}
def late_fsync(fd):
    fsynced["n"] += 1
    if fsynced["n"] > 1:
        raise OSError("injected directory fsync")
    return real_fsync(fd)
durable = b'{"generation":"durable"}\n'
os.fsync = late_fsync
try:
    try:
        io_mod.replace_bytes(dir_fd, name, durable)
    except OSError as exc:
        assert "FSYNC-FAIL" in str(exc), "directory fsync failure is untyped: %r" % exc
    else:
        raise AssertionError("directory fsync fault did not raise")
finally:
    os.fsync = real_fsync
no_stage("directory fsync fault")
assert final_bytes() == durable, "completed rename was rolled back"

fresh = b'{"generation":"fresh"}\n'
io_mod.replace_bytes(dir_fd, name, fresh)
assert final_bytes() == fresh
mode = stat.S_IMODE(os.stat(os.path.join(work, name)).st_mode)
assert mode == 0o600, oct(mode)
no_stage("retry")
print("stage-faults-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *stage-faults-ok* ]]
}

# Plan 01-06 Task 2 RED: a predictable stranded stage from a crashed prior
# attempt must never block a fresh attempt-owned stage.
@test "retry after a stranded stage from a crashed attempt succeeds" {
  local dir="$BATS_TEST_TMPDIR/stage-dir"
  mkdir -p "$dir"
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$dir" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]
dir_fd = os.open(work, os.O_RDONLY)
stranded = ".spec-006.json.%d.tmp" % os.getpid()
with open(os.path.join(work, stranded), "wb") as fh:
    fh.write(b"stranded")
payload = b'{"generation":"retry"}\n'
io_mod.replace_bytes(dir_fd, "spec-006.json", payload)
with open(os.path.join(work, "spec-006.json"), "rb") as fh:
    assert fh.read() == payload
extra = [n for n in os.listdir(work) if n.endswith(".tmp") and n != stranded]
assert not extra, "retry left its own stage behind: %r" % extra
print("stranded-retry-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *stranded-retry-ok* ]]
}

# Plan 01-06 Task 2 RED: discovery accepts only exact active names whose
# decoded root is an object with typed fields; consumed names, deceptive
# prefixes/suffixes, non-object JSON, special files, and control bytes are
# skipped or sanitized without traceback, blocking, or command execution.
@test "list skips non-object, consumed, and deceptive names without traceback" {
  local dir="$REPO/.feature-fix-swarm/takeover"
  mkdir -p "$dir"
  printf '[1,2]\n' > "$dir/spec-001.json"
  printf 'null\n' > "$dir/spec-002.json"
  printf '"scalar"\n' > "$dir/spec-003.json"
  printf '{"broken":\n' > "$dir/spec-004.json"
  printf '%s\n' '{"created_at":1,"ids":"not-a-dict","git_state":{},"resume":{}}' > "$dir/spec-005.json"
  local valid='{"created_at":1,"ids":{"run_id":"RID"},"git_state":{"branch":"b"},"resume":{"command":"echo ok"}}'
  printf '%s\n' "${valid/RID/spec-006}" > "$dir/spec-006.consumed.1.json"
  printf '%s\n' "${valid/RID/spec-06}" > "$dir/spec-06.json"
  printf '%s\n' "${valid/RID/spec-0666}" > "$dir/spec-0666.json"
  printf '%s\n' "${valid/RID/xspec-007}" > "$dir/xspec-007.json"
  printf '%s\n' "${valid/RID/spec-007}" > "$dir/spec-007.json.bak"
  printf '%s\n' "${valid/RID/spec-008}" > "$dir/.spec-008.json.99.tmp"
  printf '%s\n' "${valid/RID/spec-010}" > "$BATS_TEST_TMPDIR/outside-record"
  ln -s "$BATS_TEST_TMPDIR/outside-record" "$dir/spec-010.json"
  mkfifo "$dir/spec-011.json"
  printf '%s\n' '{"created_at":1,"ids":{"run_id":"spec-009"},"git_state":{"branch":"b\tbr\u0001anch"},"resume":{"command":"run\u001b[31m me"}}' > "$dir/spec-009.json"
  run timeout 5 env -u GATES_STORE bash "$WALL" --list
  [ "$status" -eq 0 ]
  [[ "$output" != *Traceback* ]]
  [ "$(printf '%s\n' "$output" | grep -c .)" -eq 1 ]
  [[ "$output" == spec-009$'\t'* ]]
  [ "$(printf '%s' "$output" | awk -F'\t' '{print NF}')" -eq 4 ]
  [[ "$output" != *$'\x1b'* ]]
  [[ "$output" != *consumed* ]]
  [[ "$output" != *spec-006* ]]
}

@test "runner snapshot preserves missing and malformed PID states" {
  mkdir -p "$REPO/.planning/run-state"
  printf 'running\n' > "$REPO/.planning/run-state/gsd-run.status"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  run python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
runner=json.load(open(sys.argv[1]))['runner']
assert runner['pid'] is None and runner['process_state'] == 'missing' and runner['live'] is False
PY
  [ "$status" -eq 0 ]
  printf 'not-a-pid\n' > "$REPO/.planning/run-state/gsd-run.pid"
  run env GATES_STORE="$STORE" GSD_RUN_ID=spec-006 bash "$COLLECTOR" 006
  [ "$status" -eq 0 ]
  run python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
runner=json.load(open(sys.argv[1]))['runner']
assert runner['pid'] is None and runner['process_state'] == 'malformed' and runner['live'] is False
PY
  [ "$status" -eq 0 ]
}
