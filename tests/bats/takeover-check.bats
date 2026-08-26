#!/usr/bin/env bats

# Phase 1 takeover contracts.  These intentionally exercise the real
# collector/wall boundary with a private evidence store and local git only.

setup() {
  # Hermetic supported-host identity for every fixture: managed sandboxes
  # deny `ps`/`sysctl`, so the suite supplies its own deterministic process
  # and boot identity.  Production inertness is proven by the fail-closed
  # test that unsets this seam under a denied PATH.
  export TAKEOVER_TEST_IDENTITY="bats-boot-1"
  # Hermetic run identity: never depend on an ambient GSD_RUN_ID exported by
  # the invoking shell; cases that need one set it explicitly (01-VERIFICATION
  # gap: list mode must be display-only under any ambient run id).
  unset GSD_RUN_ID
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
       "findings":[]}
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
  run env -u GATES_STORE -u GSD_RUN_ID bash "$WALL" --list
  [ "$status" -eq 0 ]
  [[ "$output" == spec-006$'\t'* ]]
  [[ "$output" == *$'\nspec-007\t'* ]]
  local baseline="$output"
  # 01-VERIFICATION gap: an ambient run id must not select records, alter
  # verdicts, or suppress rows in display-only list mode.
  run env -u GATES_STORE GSD_RUN_ID=spec-006 bash "$WALL" --list
  [ "$status" -eq 0 ]
  [ "$output" = "$baseline" ]
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

# 01-VERIFICATION gap RED: owner-lock publication is the same full-write
# transaction as staging.  A short write must complete the payload before
# fsync/link, and a zero-progress write must never publish the canonical
# lock name.
@test "owner lock short write seams complete the payload and zero progress never publishes" {
  local dir="$BATS_TEST_TMPDIR/lock-write-dir"
  mkdir -p "$dir"
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$dir" <<'PY'
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]
dir_fd = os.open(work, os.O_RDONLY)
lockname = ".takeover-check.lock"
io_mod.boot_session_id = lambda: "boot-1"
io_mod.process_identity = lambda pid: io_mod.ProcessIdentity(pid, "boot-1", "start-1", "S")
real_write = os.write
os.write = lambda fd, view: real_write(fd, bytes(view)[:7])
try:
    lock = io_mod.OwnerLock(dir_fd, "spec-006")
    assert lock._publish(), "publication failed under the short-write seam"
finally:
    os.write = real_write
payload = json.load(open(os.path.join(work, lockname)))
assert payload["pid"] == os.getpid() and payload["run_id"] == "spec-006", payload
lock.cleanup()
assert not os.path.exists(os.path.join(work, lockname))
os.write = lambda fd, view: 0
try:
    try:
        io_mod.OwnerLock(dir_fd, "spec-006")._publish()
    finally:
        os.write = real_write
except OSError:
    pass
else:
    raise AssertionError("zero-progress lock write did not raise")
assert not os.path.exists(os.path.join(work, lockname)), "zero-progress attempt published the canonical lock name"
leftovers = [n for n in os.listdir(work) if n.startswith(".takeover-lock.")]
assert not leftovers, "stranded lock temps: %r" % leftovers
print("owner-lock-write-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *owner-lock-write-ok* ]]
}

# 01-gaps2 RED: the durable intent is published through the same full-write
# transaction as staging and the owner lock — a short write completes the
# payload before fsync, and zero progress never leaves a truncated intent.
@test "durable intent short write seams complete the payload and zero progress never publishes" {
  local dir="$BATS_TEST_TMPDIR/intent-dir"
  mkdir -p "$dir"
  run python3 - "$GATES" "$dir" <<'PY'
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("takeover_gates", sys.argv[1])
g = importlib.util.module_from_spec(spec); sys.modules[spec.name] = g; spec.loader.exec_module(g)
work = sys.argv[2]
dir_fd = os.open(work, os.O_RDONLY)
name = ".takeover-transaction.spec-006.json"
payload = {"phase": "intent", "run_id": "spec-006", "pad": "x" * 96}
real_write = os.write
os.write = lambda fd, view: real_write(fd, bytes(view)[:7])
try:
    g._write_intent_excl(dir_fd, name, payload)
finally:
    os.write = real_write
with open(os.path.join(work, name), "rb") as fh:
    data = fh.read()
assert json.loads(data) == payload, "short writes were not completed: %d bytes" % len(data)
os.unlink(os.path.join(work, name))
os.write = lambda fd, view: 0
try:
    try:
        g._write_intent_excl(dir_fd, name, payload)
    finally:
        os.write = real_write
except OSError:
    pass
else:
    raise AssertionError("zero-progress intent write did not raise")
assert not os.path.exists(os.path.join(work, name)), "zero-progress attempt left a truncated durable intent"
print("intent-write-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *intent-write-ok* ]]
}

# 01-gaps2 RED: the immutable authority snapshot must retain the complete
# evidence bytes under a short-write fault and reject zero progress.
@test "authority snapshot short write seams complete the payload and zero progress is rejected" {
  run python3 - "$ROOT/scripts/gsd/takeover-transaction.py" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("takeover_transaction", sys.argv[1])
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
raw = b'{"payload":"' + b"x" * 96 + b'"}'
real_write = os.write
os.write = lambda fd, view: real_write(fd, bytes(view)[:7])
try:
    snap_fd, digest = mod._snapshot_bytes(raw)
finally:
    os.write = real_write
data = os.read(snap_fd, len(raw) + 1)
os.close(snap_fd)
assert data == raw, "short writes were not completed: %d of %d bytes" % (len(data), len(raw))
os.write = lambda fd, view: 0
try:
    try:
        mod._snapshot_bytes(raw)
    finally:
        os.write = real_write
except OSError:
    pass
else:
    raise AssertionError("zero-progress snapshot write did not raise")
print("snapshot-write-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *snapshot-write-ok* ]]
}

# 01-gaps2 RED: descriptor reads that consume payload bytes must loop to the
# exact fstat size and refuse short data instead of silently accepting it.
@test "read_regular loops short reads to the exact size and rejects early EOF" {
  local dir="$BATS_TEST_TMPDIR/read-dir"
  mkdir -p "$dir"
  python3 -c 'open("'"$dir"'/spec-006.json","w").write("{\"payload\":\"" + "x"*96 + "\"}")'
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$dir" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]
dir_fd = os.open(work, os.O_RDONLY)
with open(os.path.join(work, "spec-006.json"), "rb") as fh:
    expected = fh.read()
fd = io_mod.open_regular(dir_fd, "spec-006.json")
real_read = os.read
os.read = lambda f, n: real_read(f, min(n, 7))
try:
    data = io_mod.read_regular(fd)
finally:
    os.read = real_read
    os.close(fd)
assert data == expected, "short reads were not completed: %d of %d bytes" % (len(data), len(expected))
fd = io_mod.open_regular(dir_fd, "spec-006.json")
calls = {"n": 0}
def eof_read(f, n):
    calls["n"] += 1
    return real_read(f, min(n, 7)) if calls["n"] == 1 else b""
os.read = eof_read
try:
    try:
        io_mod.read_regular(fd)
    finally:
        os.read = real_read
        os.close(fd)
except io_mod.UnsafeTakeoverPath:
    pass
else:
    raise AssertionError("early-EOF short data was accepted")
print("read-exact-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *read-exact-ok* ]]
}

# 01-gaps2 RED: a delivered signal must release the owner lock AND terminate
# the wall with the conventional 128+signum; it must never fall through into
# continued policy execution or a success verdict after cleanup.
@test "post-exec SIGTERM releases the owner lock and terminates without a verdict" {
  write_live_takeover_fixture "$(date +%s)"
  local ev="$BATS_TEST_TMPDIR/ev-post-exec-term"; mkdir -p "$ev"
  env GATES_STORE="$STORE" TAKEOVER_TEST_PAUSE_BEFORE_CONSUME="$ev" bash "$WALL" --run-id spec-006 \
    > "$BATS_TEST_TMPDIR/wall-post-term.out" 2>&1 &
  local wall=$!
  wait_for "$ev/ready"
  [ -e "$BATS_TEST_TMPDIR/.takeover-check.lock" ]
  kill -TERM "$wall"
  local rc=0; wait "$wall" || rc=$?
  [ "$rc" -eq 143 ]
  ! grep -q 'TAKEOVER-OK' "$BATS_TEST_TMPDIR/wall-post-term.out"
  [ ! -e "$BATS_TEST_TMPDIR/.takeover-check.lock" ]
  [ -f "$(dirname "$STORE")/takeover/spec-006.json" ]
}

# 01-gaps2 RED: grant expiry must be a finite forward bound — non-finite,
# absent, or mistyped expiry values are refusals, never approvals.
@test "non-finite or absent grant expiry is denied by the pure evaluator" {
  run python3 - "$GATES" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("takeover_gates", sys.argv[1])
g = importlib.util.module_from_spec(spec); sys.modules[spec.name] = g; spec.loader.exec_module(g)
cases = {"inf": float("inf"), "nan": float("nan"), "null": None, "string": "inf", "bool": True}
for label, exp in cases.items():
    data = {"_autonomy": {"spec-006": {"grants": {"ship:gsd": {"granted_at": 1, "expires_at": exp}}}}}
    view = g.takeover_authority_view(data, "spec-006", ["ship:gsd"])
    assert view["grant_results"]["ship:gsd"] is False, "expiry %s was approved" % label
data = {"_autonomy": {"spec-006": {"grants": {"ship:gsd": {"granted_at": 1}}}}}
view = g.takeover_authority_view(data, "spec-006", ["ship:gsd"])
assert view["grant_results"]["ship:gsd"] is False, "absent expiry was approved"
print("expiry-finite-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *expiry-finite-ok* ]]
}

# 01-gaps2 RED: hostile forbid actions must be length-capped and rendered
# through the C0/C1 inert map before reaching refusal tokens or the terminal.
@test "control-bearing and oversized forbid actions render sanitized and bounded" {
  write_live_takeover_fixture "$(date +%s)"
  local record="$(dirname "$STORE")/takeover/spec-006.json"
  python3 - "$record" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as fh:
    d = json.load(fh)
d["forbid"] = [{"action": "esc\x1b[31mred\nsplit" + "A" * 200000,
                "probe": "p\x1b]0;title\x07", "reason": "r\nline"}]
with open(path, "w") as fh:
    json.dump(d, fh)
PY
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 1 ]
  [[ "$output" != *$'\x1b'* ]]
  [ "$(printf '%s\n' "$output" | grep -c '^TAKEOVER-REFUSED:poison/')" -eq 1 ]
  local reason_line
  reason_line="$(printf '%s\n' "$output" | grep '^TAKEOVER-REFUSED:poison/')"
  [ "${#reason_line}" -le 160 ]
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

# 01-VERIFICATION gap: fixtures export TAKEOVER_TEST_IDENTITY (see setup) so
# identity never shells to sandbox-forbidden `ps`/`sysctl`.  Production stays
# fail-closed: with the seam unset and the platform probes denied, identity
# is unobservable and raises instead of guessing.
@test "identity seam is deterministic for fixtures while unset seam fails closed" {
  local shim="$BATS_TEST_TMPDIR/deny-bin"
  mkdir -p "$shim"
  printf '#!/bin/sh\nexit 126\n' > "$shim/ps"
  printf '#!/bin/sh\nexit 126\n' > "$shim/sysctl"
  chmod +x "$shim/ps" "$shim/sysctl"
  run env -u TAKEOVER_TEST_IDENTITY PATH="$shim:$PATH" python3 - "$ROOT/scripts/gsd/takeover-io.py" <<'PY'
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)

# Simulate the macOS no-procfs branch on every platform so the denied `ps`
# and `sysctl` shims are the only identity sources left.
real_read_text = Path.read_text
def no_proc(self, *a, **k):
    if str(self).startswith("/proc/"):
        raise FileNotFoundError(str(self))
    return real_read_text(self, *a, **k)
Path.read_text = no_proc

for label, probe in (("boot", io_mod.boot_session_id),
                     ("process", lambda: io_mod.process_identity(os.getpid()))):
    try:
        probe()
    except io_mod.UnsafeTakeoverPath:
        pass
    else:
        raise AssertionError(label + " identity did not fail closed under denied probes")

# With the seam explicitly set, the same denied environment is deterministic.
os.environ["TAKEOVER_TEST_IDENTITY"] = "seam-boot"
assert io_mod.boot_session_id() == "seam-boot"
identity = io_mod.process_identity(os.getpid())
assert identity == io_mod.ProcessIdentity(os.getpid(), "seam-boot", "seam-boot", "S"), identity
assert io_mod.process_liveness(identity)
print("identity-seam-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *identity-seam-ok* ]]
}

# Plan 01-07 RED: the wall must hand every authority predicate one immutable
# snapshot.  The command intentionally does not exist until the GREEN change.
@test "immutable authority evaluator reads a supplied snapshot without mutating it" {
  local snapshot="$BATS_TEST_TMPDIR/snapshot.json"
  python3 - "$snapshot" <<'PY'
import hashlib,json,sys,time
now=int(time.time())
json.dump({'_autonomy':{'spec-006':{'takeover_expected':True,
  'takeover_created_at':now,'takeover_dirty_digest':hashlib.sha256(b'').hexdigest(),
  'preflight':{'pass':True,'checked_at':now},
  'grants':{'ship:gsd':{'granted_at':now,'expires_at':now+3600}}}},'findings':[]},open(sys.argv[1],'w'))
PY
  exec {snapshot_fd}<"$snapshot"
  local before
  before="$(shasum -a 256 "$snapshot")"
  run python3 "$GATES" takeover-evaluate spec-006 --snapshot-fd "$snapshot_fd" --action ship:gsd
  exec {snapshot_fd}<&-
  [ "$status" -eq 0 ]
  [[ "$output" == *'"takeover_expected": true'* ]]
  [ "$(shasum -a 256 "$snapshot")" = "$before" ]
}

@test "corrupt authority never downgrades to TAKEOVER-NONE" {
  mkdir -p "$(dirname "$STORE")"
  printf '{not-json\n' > "$STORE"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
}

# WALL-RESIDUALS e32e9020 (binding): "exactly one pre-decision evidence read"
# counts SUCCESSFUL captures consumed by the decision.  Torn-capture retries
# (<=3) are failed capture attempts, never decision reads.
@test "torn capture retries are capture attempts and one pre-decision evidence read is consumed" {
  local evidence="$BATS_TEST_TMPDIR/evidence.json"
  printf '{"_autonomy":{}}' > "$evidence"
  run python3 - "$ROOT/scripts/gsd/takeover-transaction.py" "$evidence" <<'PY'
import hashlib, importlib.util, os, sys, types
spec = importlib.util.spec_from_file_location("takeover_transaction", sys.argv[1])
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
evidence_fd = os.open(sys.argv[2], os.O_RDONLY)
real_fstat, real_read = os.fstat, os.read

def install(torn_attempts):
    calls = {"fstat": 0, "reads": 0}
    def fake_fstat(fd):
        st = real_fstat(fd)
        if fd != evidence_fd:
            return st
        calls["fstat"] += 1
        attempt = (calls["fstat"] + 1) // 2
        is_after = calls["fstat"] % 2 == 0
        if is_after and attempt <= torn_attempts:
            return types.SimpleNamespace(st_mode=st.st_mode, st_size=st.st_size,
                                         st_dev=st.st_dev, st_ino=st.st_ino,
                                         st_mtime_ns=st.st_mtime_ns + 1)
        return st
    def fake_read(fd, n):
        if fd == evidence_fd:
            calls["reads"] += 1
        return real_read(fd, n)
    os.fstat, os.read = fake_fstat, fake_read
    return calls

# Two torn attempts, then a clean capture: the decision consumes exactly one
# snapshot even though three bounded capture attempts read evidence bytes.
calls = install(2)
try:
    snap_fd, digest = mod._capture_snapshot(evidence_fd)
finally:
    os.fstat, os.read = real_fstat, real_read
assert calls["reads"] == 3, calls
with open(sys.argv[2], "rb") as fh:
    assert digest == hashlib.sha256(fh.read()).hexdigest()
os.lseek(snap_fd, 0, os.SEEK_SET)
assert os.read(snap_fd, 65536) == b'{"_autonomy":{}}'
os.close(snap_fd)

# Persistently torn: three capture attempts, zero consumed snapshots, refusal.
calls = install(4)
try:
    try:
        mod._capture_snapshot(evidence_fd)
    except ValueError:
        pass
    else:
        raise AssertionError("persistently torn capture became a snapshot")
finally:
    os.fstat, os.read = real_fstat, real_read
assert calls["reads"] == 3, calls
print("torn-capture-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *torn-capture-ok* ]]
}

# WALL-RESIDUALS 79ac0c38 (binding): the pure evaluator must APPROVE a valid
# production grant and a valid hotfix grant, alongside the denial contracts,
# without mutating the authority bytes it was handed.
@test "pure grant evaluation approves valid production and hotfix grants without mutation" {
  local snapshot="$BATS_TEST_TMPDIR/snapshot.json"
  python3 - "$snapshot" <<'PY'
import hashlib, json, sys, time
now = int(time.time())
json.dump({'_autonomy': {'spec-006': {'takeover_expected': True,
  'takeover_created_at': now, 'takeover_dirty_digest': hashlib.sha256(b'').hexdigest(),
  'preflight': {'pass': True, 'checked_at': now},
  'grants': {
    'ship:gsd': {'granted_at': now, 'expires_at': now + 3600},
    'deploy:prod-web': {'granted_at': now, 'expires_at': now + 3600},
    'hotfix:prod-web': {'granted_at': now, 'expires_at': now + 3600,
                        'reason': 'sev1 rollback'},
    'push:origin/main': {'granted_at': now - 7200, 'expires_at': now - 3600}}}},
  'findings': []}, open(sys.argv[1], 'w'))
PY
  exec {snapshot_fd}<"$snapshot"
  local before
  before="$(shasum -a 256 "$snapshot")"
  run python3 "$GATES" takeover-evaluate spec-006 --snapshot-fd "$snapshot_fd" \
    --action deploy:prod-web --action hotfix:prod-web --action migrate:prod-db
  exec {snapshot_fd}<&-
  [ "$status" -eq 0 ]
  run python3 - "$output" <<'PY'
import json, sys
results = json.loads(sys.argv[1])["grant_results"]
assert results["deploy:prod-web"] is True, results     # valid production grant approved
assert results["hotfix:prod-web"] is True, results     # valid hotfix grant approved
assert results["ship:gsd"] is True, results
assert results["push:origin/main"] is False, results   # expired ordinary grant denied
assert results["migrate:prod-db"] is False, results    # ungranted production action denied
PY
  [ "$status" -eq 0 ]
  [ "$(shasum -a 256 "$snapshot")" = "$before" ]
}

# Plan 01-07 Task 2 RED: the autonomous feature seam is the literal byte
# range between TAKEOVER-WALL-START and TAKEOVER-WALL-END in the production
# skill, executed against the real wall, never a copied approximation.
build_seam_harness() {
  mkdir -p "$REPO/scripts/gsd" "$REPO/lib"
  ln -sf "$ROOT/scripts/gsd/takeover-check.sh" "$REPO/scripts/gsd/takeover-check.sh"
  ln -sf "$ROOT/scripts/gsd/takeover-transaction.py" "$REPO/scripts/gsd/takeover-transaction.py"
  ln -sf "$ROOT/scripts/gsd/takeover-io.py" "$REPO/scripts/gsd/takeover-io.py"
  ln -sf "$ROOT/lib/gates.py" "$REPO/lib/gates.py"
  SEAM="$BATS_TEST_TMPDIR/seam.sh"
  {
    printf '#!/usr/bin/env bash\nset -uo pipefail\nRUN_ID=spec-006\n'
    printf 'echo preflight-ok\n'
    sed -n '/TAKEOVER-WALL-START/,/TAKEOVER-WALL-END/p' "$ROOT/skills/feature-implement/SKILL.md"
    printf 'echo later-execution\n'
  } > "$SEAM"
  grep -q 'TAKEOVER-WALL-START' "$SEAM"
  grep -q 'takeover-check.sh' "$SEAM"
}

@test "feature seam refusal stops after preflight and never reaches later execution" {
  build_seam_harness
  run env -u GATES_STORE python3 "$GATES" takeover-expect spec-006
  [ "$status" -eq 0 ]
  run env -u GATES_STORE bash "$SEAM"
  [ "$status" -ne 0 ]
  [ "$output" = $'preflight-ok\nTAKEOVER-REFUSED:missing-record\nUnblock (operator): /spec-status' ]
}

@test "feature seam TAKEOVER-NONE continues past preflight to later execution" {
  build_seam_harness
  run env -u GATES_STORE bash "$SEAM"
  [ "$status" -eq 0 ]
  [ "$output" = $'preflight-ok\nTAKEOVER-NONE\nlater-execution' ]
}

# Plan 01-07 Task 2 RED (REQ-105 empty edge): TAKEOVER-NONE is decided only
# against the canonical repo-rooted store.  A divergent inherited GATES_STORE
# is ignored for that decision with exactly one typed warning naming it.
@test "divergent GATES_STORE is ignored with one typed warning for TAKEOVER-NONE" {
  local override="$BATS_TEST_TMPDIR/elsewhere/evidence.json"
  mkdir -p "$(dirname "$override")"
  printf '{}' > "$override"
  run env GATES_STORE="$override" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [[ "$output" == *TAKEOVER-NONE* ]]
  [ "$(printf '%s\n' "$output" | grep -c 'TAKEOVER-WARN:gates-store-ignored')" -eq 1 ]
  [[ "$output" == *"$override"* ]]
  [ "$(env GATES_STORE="$override" bash "$WALL" --run-id spec-006 2>/dev/null)" = TAKEOVER-NONE ]
}

@test "divergent GATES_STORE cannot hide a canonical expectation behind TAKEOVER-NONE" {
  run env -u GATES_STORE python3 "$GATES" takeover-expect spec-006
  [ "$status" -eq 0 ]
  local override="$BATS_TEST_TMPDIR/elsewhere/evidence.json"
  mkdir -p "$(dirname "$override")"
  printf '{}' > "$override"
  run env GATES_STORE="$override" bash "$WALL" --run-id spec-006
  assert_single_refusal missing-record
  [ "$(printf '%s\n' "$output" | grep -c 'TAKEOVER-WARN:gates-store-ignored')" -eq 1 ]
}

@test "divergent GATES_STORE with corrupt canonical authority refuses instead of TAKEOVER-NONE" {
  mkdir -p "$REPO/.feature-fix-swarm"
  printf '{not-json\n' > "$REPO/.feature-fix-swarm/evidence.json"
  local override="$BATS_TEST_TMPDIR/elsewhere/evidence.json"
  mkdir -p "$(dirname "$override")"
  printf '{}' > "$override"
  run env GATES_STORE="$override" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
}

@test "absence evaluator failure propagates as record-mismatch never TAKEOVER-NONE" {
  mkdir -p "$(dirname "$STORE")"
  printf '{"_autonomy": []}' > "$STORE"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
}

# Plan 01-07 Task 2 RED: the wall-derived typed resume action (ship:gsd) must
# receive a pure grant verdict even when the record omits its grants row —
# record omissions add refusals, never remove checks.
@test "record omitting its ship grant row still demands a resume action verdict" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  python3 - "$STORE" "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json, sys
store = json.load(open(sys.argv[1]))
store['_autonomy']['spec-006']['grants'] = {}
json.dump(store, open(sys.argv[1], 'w'))
record = json.load(open(sys.argv[2]))
record['grants'] = []
json.dump(record, open(sys.argv[2], 'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal grant-expired
  [[ "$output" == *"--action ship:gsd"* ]]
}

# ── Plan 01-08 Task 1 RED: canonical evidence.lock, consume transaction, and
# durable cross-file crash recovery.  Event control uses file-based ready/
# release events; every wait is bounded so a missing seam fails fast.

wait_for() {
  local target="$1" ticks=$(( ${2:-4} * 20 ))
  while [ ! -e "$target" ] && [ "$ticks" -gt 0 ]; do sleep 0.05; ticks=$((ticks - 1)); done
  [ -e "$target" ]
}

@test "two event-controlled walls produce one success and one runner-live" {
  write_live_takeover_fixture "$(date +%s)"
  local ev="$BATS_TEST_TMPDIR/ev-two-walls"; mkdir -p "$ev"
  env GATES_STORE="$STORE" TAKEOVER_TEST_PAUSE_AFTER_LOCK="$ev" bash "$WALL" --run-id spec-006 \
    > "$BATS_TEST_TMPDIR/wall-one.out" 2>&1 &
  local first=$!
  wait_for "$ev/ready"
  local begin="$(date +%s)"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  assert_single_refusal runner-live
  [ "$(( $(date +%s) - begin ))" -lt 5 ]
  : > "$ev/release"
  local first_status=0; wait "$first" || first_status=$?
  [ "$first_status" -eq 0 ]
  [ "$(grep -c '^TAKEOVER-OK$' "$BATS_TEST_TMPDIR/wall-one.out")" -eq 1 ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [[ "$output" == *TAKEOVER-NONE* ]]
}

@test "injected provider live owner blocks acquisition on Linux and macOS identities" {
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$BATS_TEST_TMPDIR/lockdir-live" <<'PY'
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]; os.makedirs(work, exist_ok=True)
dfd = os.open(work, os.O_RDONLY)

def plant(payload):
    path = os.path.join(work, ".takeover-check.lock")
    try: os.unlink(path)
    except FileNotFoundError: pass
    with open(path, "w") as fh: json.dump(payload, fh)

def expect_busy(label):
    lock = io_mod.OwnerLock(dfd, "spec-006")
    try:
        lock.acquire()
    except io_mod.LockBusy:
        pass
    else:
        raise AssertionError(label + " acquired over a live owner")

io_mod.boot_session_id = lambda: "boot-1"
for label, start in (("linux", "12345"), ("macos", "Mon Aug 25 10:00:00 2026")):
    io_mod.process_identity = lambda pid, _s=start: io_mod.ProcessIdentity(pid, "boot-1", _s, "S")
    plant({"pid": 4242, "pid_start_time": start, "boot_session_id": "boot-1",
           "claimed_at": 1, "run_id": "spec-006"})
    expect_busy(label)

def unobservable(pid):
    raise io_mod.UnsafeTakeoverPath("unobservable")
io_mod.process_identity = unobservable
plant({"pid": 4242, "pid_start_time": "12345", "boot_session_id": "boot-1",
       "claimed_at": 1, "run_id": "spec-006"})
expect_busy("unprovable")

io_mod.process_identity = lambda pid: io_mod.ProcessIdentity(pid, "boot-1", "77", "S")
plant({"pid": 4242, "pid_start_time": "unknown", "boot_session_id": "boot-1",
       "claimed_at": 1, "run_id": "spec-006"})
expect_busy("unknown-recorded-start")
print("live-owner-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *live-owner-ok* ]]
}

@test "PID reuse and zombie owners are stale while prior-boot owners are reclaimed" {
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$BATS_TEST_TMPDIR/lockdir-stale" <<'PY'
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]; os.makedirs(work, exist_ok=True)
dfd = os.open(work, os.O_RDONLY)
lockpath = os.path.join(work, ".takeover-check.lock")

def plant(payload):
    try: os.unlink(lockpath)
    except FileNotFoundError: pass
    with open(lockpath, "w") as fh: json.dump(payload, fh)

def expect_reclaim(label):
    lock = io_mod.OwnerLock(dfd, "spec-006")
    lock.acquire()
    payload = json.load(open(lockpath))
    assert payload["run_id"] == "spec-006", (label, payload)
    assert set(payload) == {"pid", "pid_start_time", "boot_session_id", "claimed_at", "run_id"}, payload
    lock.cleanup()

io_mod.boot_session_id = lambda: "boot-1"
# same-PID different-start owner is stale
io_mod.process_identity = lambda pid: io_mod.ProcessIdentity(pid, "boot-1", "222", "S")
plant({"pid": 4242, "pid_start_time": "111", "boot_session_id": "boot-1",
       "claimed_at": 1, "run_id": "spec-006"})
expect_reclaim("pid-reuse")
# zombie owner is dead
io_mod.process_identity = lambda pid: io_mod.ProcessIdentity(pid, "boot-1", "111", "zombie")
plant({"pid": 4242, "pid_start_time": "111", "boot_session_id": "boot-1",
       "claimed_at": 1, "run_id": "spec-006"})
expect_reclaim("zombie")
# prior-boot owner is gone
io_mod.process_identity = lambda pid: io_mod.ProcessIdentity(pid, "boot-1", "111", "S")
plant({"pid": 4242, "pid_start_time": "111", "boot_session_id": "boot-0",
       "claimed_at": 1, "run_id": "spec-006"})
expect_reclaim("prior-boot")
print("stale-matrix-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *stale-matrix-ok* ]]
}

@test "signal cleanup removes only the owning lock inode and spares substitutes" {
  write_live_takeover_fixture "$(date +%s)"
  local ev="$BATS_TEST_TMPDIR/ev-term"; mkdir -p "$ev"
  env GATES_STORE="$STORE" TAKEOVER_TEST_PAUSE_AFTER_LOCK="$ev" bash "$WALL" --run-id spec-006 \
    > "$BATS_TEST_TMPDIR/wall-term.out" 2>&1 &
  local wall=$!
  wait_for "$ev/ready"
  [ -e "$BATS_TEST_TMPDIR/.takeover-check.lock" ]
  kill -TERM "$wall"
  local rc=0; wait "$wall" || rc=$?
  [ "$rc" -ne 0 ]
  [ ! -e "$BATS_TEST_TMPDIR/.takeover-check.lock" ]
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$BATS_TEST_TMPDIR/lockdir-sub" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]; os.makedirs(work, exist_ok=True)
dfd = os.open(work, os.O_RDONLY)
lock = io_mod.OwnerLock(dfd, "spec-006")
lock.acquire()
path = os.path.join(work, ".takeover-check.lock")
os.unlink(path)
with open(path, "w") as fh: fh.write("substitute")
lock.cleanup()
assert open(path).read() == "substitute"
print("substitute-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *substitute-ok* ]]
}

@test "replacement published during reclaim is restored and never deleted" {
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$BATS_TEST_TMPDIR/reclaim-race" <<'PY'
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]; os.makedirs(work, exist_ok=True)
dfd = os.open(work, os.O_RDONLY)
lockname = ".takeover-check.lock"
io_mod.boot_session_id = lambda: "boot-1"
io_mod.process_identity = lambda pid: (
    io_mod.ProcessIdentity(pid, "boot-1", "gone", "dead") if pid == 999
    else io_mod.ProcessIdentity(pid, "boot-1", "live-start", "S"))
stale = {"pid": 999, "pid_start_time": "x", "boot_session_id": "boot-1", "claimed_at": 1, "run_id": "spec-006"}
replacement = {"pid": 4242, "pid_start_time": "live-start", "boot_session_id": "boot-1",
               "claimed_at": 2, "run_id": "spec-007"}

def plant(payload):
    with open(os.path.join(work, lockname), "w") as fh: json.dump(payload, fh)

def swap_in_replacement():
    os.unlink(os.path.join(work, lockname))
    plant(replacement)

# pre-move identity: replacement lands after judging, before the move
plant(stale)
lock = io_mod.OwnerLock(dfd, "spec-006")
fired = {"n": 0}
def pre_move():
    if fired["n"] == 0:
        fired["n"] += 1
        swap_in_replacement()
lock._pre_move_hook = pre_move
try:
    lock.acquire()
except io_mod.LockBusy:
    pass
else:
    raise AssertionError("acquired over a live replacement")
assert json.load(open(os.path.join(work, lockname)))["run_id"] == "spec-007"

# post-move verify + link restore: replacement lands inside the move window
os.unlink(os.path.join(work, lockname))
plant(stale)
lock2 = io_mod.OwnerLock(dfd, "spec-006")
real_rename = os.rename
state = {"swapped": False}
def racy_rename(src, dst, **kw):
    if not state["swapped"] and ".takeover-check.tombstone" in dst:
        state["swapped"] = True
        swap_in_replacement()
    return real_rename(src, dst, **kw)
os.rename = racy_rename
try:
    try:
        lock2.acquire()
    except io_mod.LockBusy:
        pass
    else:
        raise AssertionError("acquired over a restored live replacement")
finally:
    os.rename = real_rename
assert json.load(open(os.path.join(work, lockname)))["run_id"] == "spec-007"
leftovers = [n for n in os.listdir(work) if "tombstone" in n]
assert not leftovers, leftovers
print("reclaim-restore-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *reclaim-restore-ok* ]]
}

@test "SIGKILL while holding the reclaim election frees it for a later contender" {
  local dir="$BATS_TEST_TMPDIR/election"; mkdir -p "$dir"
  python3 - "$dir" <<'PY' &
import fcntl, os, sys, time
work = sys.argv[1]
fd = os.open(os.path.join(work, ".takeover-check.reclaim.lock"), os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(os.path.join(work, "holder-ready"), "w").close()
time.sleep(30)
PY
  local holder=$!
  wait_for "$dir/holder-ready"
  kill -9 "$holder"
  wait "$holder" 2>/dev/null || true
  run python3 - "$ROOT/scripts/gsd/takeover-io.py" "$dir" <<'PY'
import importlib.util, json, os, sys, time
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
work = sys.argv[2]
dfd = os.open(work, os.O_RDONLY)
io_mod.boot_session_id = lambda: "boot-1"
io_mod.process_identity = lambda pid: (
    io_mod.ProcessIdentity(pid, "boot-1", "gone", "dead") if pid == 999
    else io_mod.ProcessIdentity(pid, "boot-1", "live", "S"))
with open(os.path.join(work, ".takeover-check.lock"), "w") as fh:
    json.dump({"pid": 999, "pid_start_time": "x", "boot_session_id": "boot-1",
               "claimed_at": 1, "run_id": "spec-006"}, fh)
begin = time.monotonic()
lock = io_mod.OwnerLock(dfd, "spec-006")
lock.acquire()
assert time.monotonic() - begin < 5
lock.cleanup()
with open(os.path.join(work, ".takeover-check.lock"), "w") as fh:
    json.dump({"pid": 4242, "pid_start_time": "live", "boot_session_id": "boot-1",
               "claimed_at": 2, "run_id": "spec-007"}, fh)
try:
    io_mod.OwnerLock(dfd, "spec-006").acquire()
except io_mod.LockBusy:
    pass
else:
    raise AssertionError("deleted a live replacement owner")
assert json.load(open(os.path.join(work, ".takeover-check.lock")))["run_id"] == "spec-007"
print("election-recovery-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *election-recovery-ok* ]]
}

@test "safe_path refuses a group-writable store component before lock or artifact access" {
  local base="$BATS_TEST_TMPDIR/gw"
  STORE="$base/depth/evidence.json"
  mkdir -p "$base/depth"
  write_live_takeover_fixture "$(date +%s)"
  chmod g+w "$base"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
  [ ! -e "$base/depth/.takeover-check.lock" ]
  [ -f "$base/depth/takeover/spec-006.json" ]
  chmod g-w "$base"
}

@test "pre-opened record swapped before revalidation refuses with zero success" {
  write_live_takeover_fixture "$(date +%s)"
  local ev="$BATS_TEST_TMPDIR/ev-swap"; mkdir -p "$ev"
  env GATES_STORE="$STORE" TAKEOVER_TEST_PAUSE_AFTER_LOCK="$ev" bash "$WALL" --run-id spec-006 \
    > "$BATS_TEST_TMPDIR/wall-swap.out" 2>&1 &
  local wall=$!
  wait_for "$ev/ready"
  local record="$(dirname "$STORE")/takeover/spec-006.json"
  mv "$record" "$record.peer"
  cp "$record.peer" "$record"
  : > "$ev/release"
  local rc=0; wait "$wall" || rc=$?
  [ "$rc" -ne 0 ]
  grep -q 'TAKEOVER-REFUSED:record-mismatch' "$BATS_TEST_TMPDIR/wall-swap.out"
  ! grep -q 'TAKEOVER-OK' "$BATS_TEST_TMPDIR/wall-swap.out"
  [ -f "$record" ]
  [ -z "$(find "$(dirname "$STORE")/takeover" -name 'spec-006.consumed.*' -print -quit)" ]
}

@test "ordinary writer holding canonical evidence.lock forces a bounded pre-rename refusal" {
  write_live_takeover_fixture "$(date +%s)"
  local hold="$BATS_TEST_TMPDIR/hold"; mkdir -p "$hold"
  env GATES_STORE="$STORE" GATES_TEST_HOLD_LOCK="$hold" python3 "$GATES" note-degraded rung-attempt \
    --rung-id test:test:test --outcome ok > /dev/null 2>&1 &
  local writer=$!
  wait_for "$hold/held"
  local begin="$(date +%s)"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
  [ "$(( $(date +%s) - begin ))" -lt 5 ]
  [ -f "$(dirname "$STORE")/takeover/spec-006.json" ]
  [ ! -e "$(dirname "$STORE")/takeover/.takeover-transaction.spec-006.json" ]
  run python3 - "$STORE" <<'PY'
import json,sys
row=json.load(open(sys.argv[1]))['_autonomy']['spec-006']
assert row['takeover_expected'] is True and 'takeover_consumed_at' not in row
PY
  [ "$status" -eq 0 ]
  run python3 - "$(dirname "$STORE")/evidence.lock" "$(cat "$hold/held")" <<'PY'
import os,sys
st=os.stat(sys.argv[1])
assert "%d:%d" % (st.st_dev, st.st_ino) == sys.argv[2], (st.st_dev, st.st_ino, sys.argv[2])
PY
  [ "$status" -eq 0 ]
  : > "$hold/release"
  local rc=0; wait "$writer" || rc=$?
  [ "$rc" -eq 0 ]
}

@test "ordinary writer after consume blocks until commit and keeps the consumed expectation" {
  write_live_takeover_fixture "$(date +%s)"
  local ev="$BATS_TEST_TMPDIR/ev-locked"; mkdir -p "$ev"
  env GATES_STORE="$STORE" TAKEOVER_TEST_PAUSE_LOCKED="$ev" bash "$WALL" --run-id spec-006 \
    > "$BATS_TEST_TMPDIR/wall-locked.out" 2>&1 &
  local wall=$!
  wait_for "$ev/held"
  env GATES_STORE="$STORE" python3 "$GATES" note-degraded rung-attempt \
    --rung-id test:test:test --outcome ok > "$BATS_TEST_TMPDIR/writer.out" 2>&1 &
  local writer=$!
  sleep 0.5
  run python3 - "$STORE" <<'PY'
import json,sys
assert '_degradation' not in json.load(open(sys.argv[1]))
PY
  [ "$status" -eq 0 ]
  : > "$ev/release"
  local rc=0; wait "$wall" || rc=$?
  [ "$rc" -eq 0 ]
  grep -q '^TAKEOVER-OK$' "$BATS_TEST_TMPDIR/wall-locked.out"
  rc=0; wait "$writer" || rc=$?
  [ "$rc" -eq 0 ]
  run python3 - "$STORE" "$(cat "$ev/held")" "$(dirname "$STORE")/evidence.lock" <<'PY'
import json,os,sys
d=json.load(open(sys.argv[1]))
row=d['_autonomy']['spec-006']
assert row['takeover_expected'] is False
assert isinstance(row.get('takeover_consumed_at'), int)
assert 'test:test:test' in json.dumps(d['_degradation'])
st=os.stat(sys.argv[3])
assert "%d:%d" % (st.st_dev, st.st_ino) == sys.argv[2]
PY
  [ "$status" -eq 0 ]
}

@test "stale snapshot digest mismatch preserves the concurrent update and a rerun redecides" {
  write_live_takeover_fixture "$(date +%s)"
  local ev="$BATS_TEST_TMPDIR/ev-stale"; mkdir -p "$ev"
  env GATES_STORE="$STORE" TAKEOVER_TEST_PAUSE_AFTER_LOCK="$ev" bash "$WALL" --run-id spec-006 \
    > "$BATS_TEST_TMPDIR/wall-stale.out" 2>&1 &
  local wall=$!
  wait_for "$ev/ready"
  run env GATES_STORE="$STORE" python3 "$GATES" note-degraded rung-attempt --rung-id test:test:test --outcome ok
  [ "$status" -eq 0 ]
  : > "$ev/release"
  local rc=0; wait "$wall" || rc=$?
  [ "$rc" -ne 0 ]
  grep -q 'TAKEOVER-REFUSED:record-mismatch' "$BATS_TEST_TMPDIR/wall-stale.out"
  [ -f "$(dirname "$STORE")/takeover/spec-006.json" ]
  run python3 - "$STORE" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert 'test:test:test' in json.dumps(d.get('_degradation', {}))
assert d['_autonomy']['spec-006']['takeover_expected'] is True
PY
  [ "$status" -eq 0 ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
}

@test "caught pre-rename fault and post-rename fault recover to one canonical pair" {
  write_live_takeover_fixture "$(date +%s)"
  run python3 - "$GATES" "$STORE" <<'PY'
import hashlib, importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("gates_lib", sys.argv[1])
gates = importlib.util.module_from_spec(spec); sys.modules[spec.name] = gates; spec.loader.exec_module(gates)
store = sys.argv[2]; store_dir = os.path.dirname(store)
takeover = os.path.join(store_dir, "takeover")

def consume(emit):
    sdfd = os.open(store_dir, os.O_RDONLY)
    tfd = os.open(takeover, os.O_RDONLY)
    rfd = os.open(os.path.join(takeover, "spec-006.json"), os.O_RDONLY)
    with open(store, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    try:
        return gates.takeover_consume("spec-006", 1700000000, sdfd, None, tfd, rfd,
                                      "spec-006.json", sha, 1000, emit=emit)
    finally:
        for fd in (rfd, tfd, sdfd):
            os.close(fd)

def fail_nth(name, n):
    real = getattr(os, name)
    state = {"n": 0}
    def wrap(*a, **k):
        state["n"] += 1
        if state["n"] == n:
            raise OSError("injected %s fault" % name)
        return real(*a, **k)
    return real, wrap

original = open(store, "rb").read()

# pre-rename fault: the provisional record rename fails; nothing may mutate
real, wrap = fail_nth("rename", 1); os.rename = wrap
emitted = []
try:
    out = consume(emitted.append)
finally:
    os.rename = real
assert out == {"outcome": "refused", "reason": "record-mismatch"}, out
assert not emitted, emitted
assert open(store, "rb").read() == original
names = set(n for n in os.listdir(takeover) if not n.endswith(".md"))
assert names == {"spec-006.json"}, names

# post-rename fault: intent phase advance fails after the provisional rename;
# recovery restores active plus original
real, wrap = fail_nth("replace", 1); os.replace = wrap
emitted = []
try:
    out = consume(emitted.append)
finally:
    os.replace = real
assert out == {"outcome": "refused", "reason": "record-mismatch"}, out
assert open(store, "rb").read() == original
names = set(n for n in os.listdir(takeover) if not n.endswith(".md"))
assert names == {"spec-006.json"}, names

# final-rename fault: evidence already desired; recovery rolls forward and
# acknowledges the recovered success
real, wrap = fail_nth("rename", 2); os.rename = wrap
emitted = []
try:
    out = consume(emitted.append)
finally:
    os.rename = real
assert out["outcome"] == "ok", out
assert "TAKEOVER-OK" in emitted, emitted
row = json.load(open(store))["_autonomy"]["spec-006"]
assert row["takeover_expected"] is False and row["takeover_consumed_at"] == 1700000000
names = [n for n in os.listdir(takeover) if not n.endswith(".md")]
assert not os.path.exists(os.path.join(takeover, "spec-006.json"))
consumed = [n for n in names if n.startswith("spec-006.consumed.")]
assert len(consumed) == 1 and len(names) == 1, names
print("caught-faults-ok")
PY
  [ "$status" -eq 0 ]
  [[ "$output" == *caught-faults-ok* ]]
}

@test "transaction recovery restores active plus original after SIGKILL after the provisional rename" {
  write_live_takeover_fixture "$(date +%s)"
  local takeover="$(dirname "$STORE")/takeover"
  run env GATES_STORE="$STORE" TAKEOVER_KILL_AT=after-provisional-rename bash "$WALL" --run-id spec-006
  [ "$status" -ne 0 ]
  [ -f "$takeover/.takeover-transaction.spec-006.json" ]
  [ ! -e "$takeover/spec-006.json" ]
  local begin="$(date +%s)"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$(( $(date +%s) - begin ))" -lt 5 ]
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
  [ ! -e "$takeover/.takeover-transaction.spec-006.json" ]
  [ -n "$(find "$takeover" -name 'spec-006.consumed.*.json' -print -quit)" ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [[ "$output" == *TAKEOVER-NONE* ]]
}

@test "transaction recovery completes consumed plus cleared after SIGKILL after evidence replacement" {
  write_live_takeover_fixture "$(date +%s)"
  local takeover="$(dirname "$STORE")/takeover"
  run env GATES_STORE="$STORE" TAKEOVER_KILL_AT=after-evidence-replace bash "$WALL" --run-id spec-006
  [ "$status" -ne 0 ]
  [ -f "$takeover/.takeover-transaction.spec-006.json" ]
  local begin="$(date +%s)"
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$(( $(date +%s) - begin ))" -lt 5 ]
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
  run python3 - "$STORE" <<'PY'
import json,sys
row=json.load(open(sys.argv[1]))['_autonomy']['spec-006']
assert row['takeover_expected'] is False
assert isinstance(row.get('takeover_consumed_at'), int)
PY
  [ "$status" -eq 0 ]
  [ ! -e "$takeover/.takeover-transaction.spec-006.json" ]
  [ -n "$(find "$takeover" -name 'spec-006.consumed.*.json' -print -quit)" ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [[ "$output" == *TAKEOVER-NONE* ]]
}

@test "transaction recovery rolls forward after SIGKILL before the final rename" {
  write_live_takeover_fixture "$(date +%s)"
  local takeover="$(dirname "$STORE")/takeover"
  run env GATES_STORE="$STORE" TAKEOVER_KILL_AT=before-final-rename bash "$WALL" --run-id spec-006
  [ "$status" -ne 0 ]
  [ -f "$takeover/.takeover-transaction.spec-006.json" ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
  [ ! -e "$takeover/.takeover-transaction.spec-006.json" ]
  [ -n "$(find "$takeover" -name 'spec-006.consumed.*.json' -print -quit)" ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [[ "$output" == *TAKEOVER-NONE* ]]
}

@test "completed intent is acknowledged and re-emits TAKEOVER-OK before deletion" {
  local takeover="$(dirname "$STORE")/takeover"
  for hook in after-record-consumed after-acknowledged; do
    rm -rf "$takeover" "$STORE" "$(dirname "$STORE")/evidence.lock"
    write_live_takeover_fixture "$(date +%s)"
    run env GATES_STORE="$STORE" TAKEOVER_KILL_AT="$hook" bash "$WALL" --run-id spec-006
    [ "$status" -ne 0 ]
    [ -f "$takeover/.takeover-transaction.spec-006.json" ]
    run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
    [ "$status" -eq 0 ]
    [ "$output" = TAKEOVER-OK ]
    [ ! -e "$takeover/.takeover-transaction.spec-006.json" ]
    run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
    [ "$status" -eq 0 ]
    [[ "$output" == *TAKEOVER-NONE* ]]
  done
}

@test "legitimate locked writer supersedes a crashed intent and the rerun proceeds normally" {
  write_live_takeover_fixture "$(date +%s)"
  local takeover="$(dirname "$STORE")/takeover"
  run env GATES_STORE="$STORE" TAKEOVER_KILL_AT=after-provisional-rename bash "$WALL" --run-id spec-006
  [ "$status" -ne 0 ]
  run env GATES_STORE="$STORE" python3 "$GATES" note-degraded rung-attempt --rung-id test:test:test --outcome ok
  [ "$status" -eq 0 ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -c '^TAKEOVER-SUPERSEDED$')" -eq 1 ]
  [[ "$output" == *'Unblock (operator): bash scripts/gsd/takeover-check.sh --run-id spec-006'* ]]
  [ -f "$takeover/spec-006.json" ]
  [ ! -e "$takeover/.takeover-transaction.spec-006.json" ]
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
}

@test "unexplained in-place mutation refuses record-mismatch and transaction recovery retains the intent" {
  write_live_takeover_fixture "$(date +%s)"
  local takeover="$(dirname "$STORE")/takeover"
  run env GATES_STORE="$STORE" TAKEOVER_KILL_AT=after-provisional-rename bash "$WALL" --run-id spec-006
  [ "$status" -ne 0 ]
  python3 - "$STORE" <<'PY'
import sys
with open(sys.argv[1], "r+") as fh:
    fh.write('{"_autonomy": {"spec-006": {"takeover_expected": true, "tampered": true}}}')
    fh.truncate()
PY
  run env GATES_STORE="$STORE" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
  [ -f "$takeover/.takeover-transaction.spec-006.json" ]
  [ ! -e "$takeover/spec-006.json" ]
}

@test "under-lock policy re-gate refuses -uall untracked drift before any mutation" {
  grep -q -- '--untracked-files=all' "$GATES"
  write_live_takeover_fixture "$(date +%s)"
  local ev="$BATS_TEST_TMPDIR/ev-regate"; mkdir -p "$ev"
  env GATES_STORE="$STORE" TAKEOVER_TEST_PAUSE_BEFORE_CONSUME="$ev" bash "$WALL" --run-id spec-006 \
    > "$BATS_TEST_TMPDIR/wall-regate.out" 2>&1 &
  local wall=$!
  wait_for "$ev/ready"
  mkdir -p "$REPO/untracked-dir"
  printf 'drift\n' > "$REPO/untracked-dir/inner.txt"
  : > "$ev/release"
  local rc=0; wait "$wall" || rc=$?
  [ "$rc" -ne 0 ]
  grep -q 'TAKEOVER-REFUSED:dirty-worktree' "$BATS_TEST_TMPDIR/wall-regate.out"
  local takeover="$(dirname "$STORE")/takeover"
  [ -f "$takeover/spec-006.json" ]
  [ ! -e "$takeover/.takeover-transaction.spec-006.json" ]
  run python3 - "$STORE" <<'PY'
import json,sys
assert json.load(open(sys.argv[1]))['_autonomy']['spec-006']['takeover_expected'] is True
PY
  [ "$status" -eq 0 ]
}

@test "success under canonical evidence.lock consumes one pre-decision and one post-decision read" {
  write_live_takeover_fixture "$(date +%s)"
  local log="$BATS_TEST_TMPDIR/reads.log"
  run env GATES_STORE="$STORE" TAKEOVER_READ_LOG="$log" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
  [ "$(cat "$log")" = $'pre-decision\npost-decision' ]
  rm -f "$log"
  rm -rf "$(dirname "$STORE")/takeover"
  python3 - "$STORE" <<'PY'
import json,sys
json.dump({'_autonomy':{'spec-006':{'takeover_expected':True}}},open(sys.argv[1],'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_READ_LOG="$log" bash "$WALL" --run-id spec-006
  assert_single_refusal missing-record
  [ "$(cat "$log")" = "pre-decision" ]
}

# ── Plan 01-08 Task 2 RED: exact Git identity and the complete otherwise-valid
# refusal/remedy matrix.  Every case starts from a fully valid baseline and
# mutates exactly one invariant so no earlier check can cause the refusal.

reset_fixture_state() {
  rm -rf "$(dirname "$STORE")/takeover"
  rm -f "$STORE" "$(dirname "$STORE")/evidence.lock" "$(dirname "$STORE")/.takeover-check.lock"
}

@test "runner-live refuses an otherwise-valid record while the recorded owner is live" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  sleep 30 &
  local keeper=$!
  python3 - "$ROOT/scripts/gsd/takeover-io.py" "$(dirname "$STORE")/.takeover-check.lock" "$keeper" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("takeover_io", sys.argv[1])
io_mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = io_mod; spec.loader.exec_module(io_mod)
pid = int(sys.argv[3])
identity = io_mod.process_identity(pid)
payload = {"pid": pid, "pid_start_time": identity.pid_start_time or "unknown",
           "boot_session_id": identity.boot_session_id, "claimed_at": 1, "run_id": "spec-006"}
with open(sys.argv[2], "w") as fh:
    json.dump(payload, fh)
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  kill "$keeper" 2>/dev/null || true
  assert_single_refusal runner-live
  [[ "$output" == *'Unblock (operator): bash scripts/gsd/takeover-check.sh --run-id spec-006'* ]]
  [ -f "$(dirname "$STORE")/takeover/spec-006.json" ]
}

@test "mid-rebase refuses an otherwise-valid record with the exact continue remedy" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  mkdir -p "$REPO/.git/rebase-merge"
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal mid-rebase
  [[ "$output" == *'Unblock (operator): git rebase --continue'* ]]
  rm -rf "$REPO/.git/rebase-merge"
}

@test "linked worktree rebase state is resolved through git rev-parse --git-path" {
  local now="$(date +%s)"
  local wt="$BATS_TEST_TMPDIR/wt"
  git worktree add -q -b wt-branch "$wt"
  cd "$wt"
  write_live_takeover_fixture "$now"
  python3 - "$(dirname "$STORE")/takeover/spec-006.json" "$(git branch --show-current)" "$(git rev-parse HEAD)" <<'PY'
import json,sys
p,branch,head=sys.argv[1:]
d=json.load(open(p)); d['git_state']['branch']=branch; d['git_state']['head']=head
json.dump(d,open(p,'w'))
PY
  mkdir -p "$REPO/.git/worktrees/wt/rebase-apply"
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal mid-rebase
}

@test "git administrative path query failure fails closed as record-mismatch" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  local realgit; realgit="$(command -v git)"
  local stub="$BATS_TEST_TMPDIR/gitstub"; mkdir -p "$stub"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'for arg in "$@"; do [ "$arg" = "--git-path" ] && exit 128; done\n'
    printf 'exec %s "$@"\n' "$realgit"
  } > "$stub/git"
  chmod +x "$stub/git"
  run env PATH="$stub:$PATH" GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal record-mismatch
}

@test "branch-gone covers renamed local branch, missing upstream, and named HEAD mismatch" {
  local now="$(date +%s)"
  # recorded local branch no longer exists
  write_live_takeover_fixture "$now"
  git branch -m 006-takeover 006-renamed
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal branch-gone
  [[ "$output" == *'Unblock (operator): git fetch --prune origin'* ]]
  git branch -m 006-renamed 006-takeover
  # missing recorded upstream ref refuses without a crash or network call
  reset_fixture_state
  write_live_takeover_fixture "$now"
  python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d['git_state']['upstream']='refs/remotes/origin/nope'
json.dump(d,open(p,'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal branch-gone
  # named branch with a different recorded full HEAD
  reset_fixture_state
  write_live_takeover_fixture "$now"
  python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d['git_state']['head']='a'*40
json.dump(d,open(p,'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal branch-gone
}

@test "recorded detached HEAD must match the exact current commit" {
  local now="$(date +%s)"
  # valid recorded detached state at the exact current commit passes
  write_live_takeover_fixture "$now"
  python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d['git_state']['branch']=''
json.dump(d,open(p,'w'))
PY
  git checkout -q --detach
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
  # empty recorded branch with a different full HEAD refuses branch-gone
  git checkout -q 006-takeover
  git commit -q --allow-empty -m drift
  reset_fixture_state
  write_live_takeover_fixture "$now"
  python3 - "$(dirname "$STORE")/takeover/spec-006.json" "$(git rev-parse 'HEAD~1')" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p)); d['git_state']['branch']=''; d['git_state']['head']=sys.argv[2]
json.dump(d,open(p,'w'))
PY
  git checkout -q --detach
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal branch-gone
  git checkout -q 006-takeover
}

@test "older record mtime and older ledger grant anchor each defeat a forged created_at" {
  local now="$(date +%s)"
  # fresh created_at, old record artifact mtime
  write_live_takeover_fixture "$now"
  touch -t 202501010000 "$(dirname "$STORE")/takeover/spec-006.json"
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal grant-expired
  [[ "$output" == *'Unblock (operator): /spec-status'* ]]
  # fresh created_at, old ledger grant anchor
  reset_fixture_state
  write_live_takeover_fixture "$now"
  python3 - "$STORE" "$(dirname "$STORE")/takeover/spec-006.json" "$now" <<'PY'
import json,sys
old=int(sys.argv[3])-259300
store=json.load(open(sys.argv[1]))
store['_autonomy']['spec-006']['grants']['ship:gsd']['granted_at']=old
json.dump(store,open(sys.argv[1],'w'))
d=json.load(open(sys.argv[2]))
for row in d['grants']:
    if row['action']=='ship:gsd': row['granted_at']=old
json.dump(d,open(sys.argv[2],'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal grant-expired
  [[ "$output" == *'Unblock (operator): /spec-status'* ]]
}

@test "a genuinely expired grant refuses with the exact re-grant remedy and no mutation" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  python3 - "$STORE" "$(dirname "$STORE")/takeover/spec-006.json" "$now" <<'PY'
import json,sys
exp=int(sys.argv[3])-10
store=json.load(open(sys.argv[1]))
store['_autonomy']['spec-006']['grants']['ship:gsd']['expires_at']=exp
json.dump(store,open(sys.argv[1],'w'))
d=json.load(open(sys.argv[2]))
for row in d['grants']:
    if row['action']=='ship:gsd': row['expires_at']=exp
json.dump(d,open(sys.argv[2],'w'))
PY
  local before; before="$(shasum -a 256 "$STORE" | awk '{print $1}')"
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal grant-expired
  [[ "$output" == *"--action ship:gsd"* ]]
  [ "$(shasum -a 256 "$STORE" | awk '{print $1}')" = "$before" ]
}

@test "findings-open refuses on an otherwise-valid record with one unresolved HIGH finding" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  # 01-gaps3 CR-01: seed through the PRODUCTION writer, never a private
  # namespace — the queue `findings-queue add` persists is the queue the
  # wall's authority evaluator must read.
  run env GATES_STORE="$STORE" python3 "$GATES" findings-queue add src/x.py 'open finding' --severity HIGH --run-id spec-006
  [ "$status" -eq 0 ]
  # The pure evaluator must surface the production-queued finding.
  exec {snapshot_fd}<"$STORE"
  run python3 "$GATES" takeover-evaluate spec-006 --snapshot-fd "$snapshot_fd" --action ship:gsd
  exec {snapshot_fd}<&-
  [ "$status" -eq 0 ]
  run python3 - "$output" <<'PY'
import json,sys
rows=json.loads(sys.argv[1])['unresolved_findings']
assert len(rows)==1 and rows[0]['severity']=='HIGH' and not rows[0]['resolved'], rows
PY
  [ "$status" -eq 0 ]
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  assert_single_refusal findings-open
  [[ "$output" == *'findings-queue list --unresolved'* ]]
}

@test "pure evaluator fails closed on a non-conforming findings namespace" {
  local snapshot="$BATS_TEST_TMPDIR/snapshot.json"
  for shape in '"nope"' '[1]' '[{"severity":"HIGH","resolved":false},null]'; do
    python3 - "$snapshot" "$shape" <<'PY'
import json,sys,time
now=int(time.time())
json.dump({'_autonomy':{'spec-006':{'takeover_expected':True,
  'preflight':{'pass':True,'checked_at':now},
  'grants':{'ship:gsd':{'granted_at':now,'expires_at':now+3600}}}},
  'findings':json.loads(sys.argv[2])},open(sys.argv[1],'w'))
PY
    exec {snapshot_fd}<"$snapshot"
    run python3 "$GATES" takeover-evaluate spec-006 --snapshot-fd "$snapshot_fd" --action ship:gsd
    exec {snapshot_fd}<&-
    [ "$status" -eq 1 ] || { echo "shape=$shape unexpectedly passed: $output" >&3; return 1; }
    [[ "$output" == *'TAKEOVER-EVALUATE-REJECTED'* ]] || { echo "shape=$shape output: $output" >&3; return 1; }
    [[ "$output" != *Traceback* ]]
  done
}

@test "poison action refuses with a shell-quoted rendered remedy that never executes" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  python3 - "$(dirname "$STORE")/takeover/spec-006.json" <<'PY'
import json,sys
p=sys.argv[1]; d=json.load(open(p))
d['forbid']=[{'action':'push origin main','probe':'mid-rebase','reason':'forbidden'}]
json.dump(d,open(p,'w'))
PY
  run env GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  [ "$status" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -c '^TAKEOVER-REFUSED:poison/push origin main$')" -eq 1 ]
  [[ "$output" == *"--action 'push origin main'"* ]]
  [[ "$output" == *"'takeover record forbids action'"* ]]
  [ -f "$(dirname "$STORE")/takeover/spec-006.json" ]
}

@test "own live ship grant passes and liveness-check.sh is never invoked" {
  local now="$(date +%s)"
  write_live_takeover_fixture "$now"
  local stub="$BATS_TEST_TMPDIR/liveness-stub"; mkdir -p "$stub"
  printf '#!/usr/bin/env bash\ntouch "%s/liveness-called"\n' "$stub" > "$stub/liveness-check.sh"
  chmod +x "$stub/liveness-check.sh"
  run env PATH="$stub:$PATH" GATES_STORE="$STORE" TAKEOVER_NOW="$((now + 1))" bash "$WALL" --run-id spec-006
  [ "$status" -eq 0 ]
  [ "$output" = TAKEOVER-OK ]
  [ ! -e "$stub/liveness-called" ]
  ! grep -q 'liveness-check' "$WALL"
}
