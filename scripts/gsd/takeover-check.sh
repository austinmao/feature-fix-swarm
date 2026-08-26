#!/usr/bin/env bash
# Deterministic takeover wall.  Records are hostile discovery data, never authority.
set -uo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GP="$SCRIPT_ROOT/lib/gates.py"
RUN_ID="${GSD_RUN_ID:-}"; MODE=text; LIST=0
ORIGINAL_ARGS=("$@")
usage() { echo "usage: takeover-check.sh --run-id spec-NNN [--json] | --list" >&2; exit 2; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --run-id) [ "$#" -ge 2 ] || usage; RUN_ID="$2"; shift 2 ;;
    --json) MODE=json; shift ;;
    --list) LIST=1; shift ;;
    *) usage ;;
  esac
done
[ -f "$GP" ] || { echo 'TAKEOVER-REFUSED:decoy-store'; exit 1; }
# List mode is display-only metadata: an ambient GSD_RUN_ID must never select
# records, drive recovery, or acquire the owner lock (01-VERIFICATION gap).
if [ "$LIST" -eq 1 ]; then RUN_ID=""; fi
[ "$LIST" -eq 1 ] || [[ "$RUN_ID" =~ ^spec-[0-9]{3}$ ]] || usage

# Store selection is configuration: producer and consumer inherit one
# legitimate GATES_STORE and derive all authority from that same resolver.
gate() {
  case "$1" in
    takeover-state|takeover-expectation|check-preflight|check-grant)
      if [ -n "${TAKEOVER_STORE_DIR_FD:-}" ] && [ -n "${TAKEOVER_EVIDENCE_FD:-}" ]; then
        python3 "$GP" "$@" --store-dir-fd "$TAKEOVER_STORE_DIR_FD" --store-fd "$TAKEOVER_EVIDENCE_FD"
        return
      fi ;;
  esac
  python3 "$GP" "$@"
}
evaluate() {
  [ -n "${TAKEOVER_SNAPSHOT_FD:-}" ] || return 1
  python3 "$GP" takeover-evaluate "$RUN_ID" --snapshot-fd "$TAKEOVER_SNAPSHOT_FD" "$@"
}
sq() { python3 -c 'import shlex,sys; print(shlex.quote(sys.argv[1]))' "$1"; }
remedy_for() {
  case "$1" in
    env-mismatch) printf 'unset GATES_STORE' ;; decoy-store|missing-record|record-mismatch|stale-record) printf '/spec-status' ;;
    runner-live) printf 'bash scripts/gsd/takeover-check.sh --run-id %s' "$(sq "$RUN_ID")" ;;
    mid-rebase) printf 'git rebase --continue' ;; branch-gone) printf 'git fetch --prune origin' ;;
    dirty-worktree) printf '/adopt-wip' ;; preflight-stale) printf '/preflight' ;;
    grant-expired) if [ "${2:-}" = stale-record ]; then printf '/spec-status'; else printf 'python3 %s grant %s --action %s' "$(sq "$GP")" "$(sq "$RUN_ID")" "$(sq "${2:-ship:gsd}")"; fi ;;
    findings-open) printf 'python3 %s findings-queue list --unresolved' "$(sq "$GP")" ;;
    poison/*) printf "python3 %s pending %s --action %s --reason 'takeover record forbids action'" "$(sq "$GP")" "$(sq "$RUN_ID")" "$(sq "${1#poison/}")" ;;
    *) printf '/spec-status' ;;
  esac
}
refuse() {
  local reason="$1" remedy; remedy="$(remedy_for "$reason" "${2:-}")"
  if [ "$MODE" = json ]; then
    python3 - "$RUN_ID" "$reason" "$remedy" <<'PY'
import json,sys
print(json.dumps({'schema_version':1,'run_id':sys.argv[1],'verdict':'TAKEOVER-REFUSED','reason':sys.argv[2],'remedy':sys.argv[3]},separators=(',',':')))
PY
  else
    printf 'TAKEOVER-REFUSED:%s\nUnblock (operator): %s\n' "$reason" "$remedy"
  fi
  exit 1
}

CANON_STORE="$(gate store-path 2>/dev/null)" || refuse decoy-store
if [ "${TAKEOVER_TRANSACTION:-}" != 1 ]; then
  exec python3 "$SCRIPT_ROOT/scripts/gsd/takeover-transaction.py" \
    --script "$0" --store "$CANON_STORE" --run-id "$RUN_ID" -- "${ORIGINAL_ARGS[@]}" || refuse record-mismatch
fi
if [ "$LIST" -eq 1 ]; then
  [ -n "${TAKEOVER_DIR_FD:-}" ] || exit 0
  python3 "$SCRIPT_ROOT/scripts/gsd/takeover-io.py" list --takeover-fd "$TAKEOVER_DIR_FD"
  exit 0
fi
expectation_present() {
  local expectation
  expectation="$(evaluate 2>/dev/null)" || return 2
  python3 - "$expectation" <<'PY'
import json,sys
value=json.loads(sys.argv[1]).get('takeover_expected')
raise SystemExit(0 if value is True else 1)
PY
}
# Read-only canonical-store probe for the absence verdict.  It reuses the
# takeover-io no-follow walk so no pathname open of evidence.json happens
# outside the trusted-path discipline (WALL-RESIDUALS 6be15e6c).
canonical_absence_state() {
  python3 - "$SCRIPT_ROOT/scripts/gsd/takeover-io.py" "$1" "$RUN_ID" <<'PY'
import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location("takeover_io",sys.argv[1])
io_mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=io_mod; spec.loader.exec_module(io_mod)
store,run_id=sys.argv[2],sys.argv[3]
def out(v):
    print(v); raise SystemExit(0)
try:
    try:
        dir_fd=io_mod.open_store_directory(store)
    except (FileNotFoundError,NotADirectoryError):
        out('NONE')
    takeover_fd=io_mod.open_takeover(dir_fd)
    if takeover_fd is not None:
        try:
            os.close(io_mod.open_regular(takeover_fd,run_id+'.json'))
            out('RECORD')
        except FileNotFoundError:
            pass
    try:
        evidence_fd=io_mod.open_regular(dir_fd,'evidence.json')
    except FileNotFoundError:
        out('NONE')
    data=json.loads(io_mod.read_regular(evidence_fd).decode('utf-8'))
    if not isinstance(data,dict): out('CORRUPT')
    auto=data.get('_autonomy',{})
    if not isinstance(auto,dict): out('CORRUPT')
    row=auto.get(run_id,{})
    if not isinstance(row,dict): out('CORRUPT')
    out('EXPECTED' if row.get('takeover_expected') is True else 'NONE')
except SystemExit:
    raise
except Exception:
    out('CORRUPT')
PY
}
none_or_missing() {
  if expectation_present; then
    refuse missing-record
  elif [ "$?" -eq 2 ]; then
    refuse record-mismatch
  fi
  # REQ-105 empty edge: TAKEOVER-NONE is decided only against the canonical
  # repo-rooted store.  A divergent inherited GATES_STORE cannot manufacture
  # the no-op verdict; it is ignored for this decision with exactly one typed
  # warning naming the ignored value.  Expectation or authority failure in
  # the canonical store never downgrades to NONE.
  if [ -n "${GATES_STORE:-}" ]; then
    local canonical_default canonical_state
    canonical_default="$(env -u GATES_STORE python3 "$GP" store-path 2>/dev/null)" || refuse record-mismatch
    if [ "$canonical_default" != "$CANON_STORE" ]; then
      printf 'TAKEOVER-WARN:gates-store-ignored %s\n' "$GATES_STORE" >&2
      canonical_state="$(canonical_absence_state "$canonical_default")" || refuse record-mismatch
      case "$canonical_state" in
        NONE) ;;
        EXPECTED) refuse missing-record ;;
        *) refuse record-mismatch ;;
      esac
    fi
  fi
  if [ "$MODE" = json ]; then
    python3 - "$RUN_ID" <<'PY'
import json,sys
print(json.dumps({'schema_version':1,'run_id':sys.argv[1],'verdict':'TAKEOVER-NONE','reason':None,'remedy':None},separators=(',',':')))
PY
  else
    echo TAKEOVER-NONE
  fi
  exit 0
}
if [ -z "${TAKEOVER_DIR_FD:-}" ]; then
  # With no record, an expectation bit is the only ledger fact that is
  # relevant. Do not start the live authority catalog on this no-op branch.
  none_or_missing
fi
if [ -z "${TAKEOVER_RECORD_FD:-}" ]; then
  none_or_missing
fi

# Validate exactly the bytes opened once. O_NONBLOCK makes FIFO/device input
# bounded; O_NOFOLLOW and fstat close both symlink and type confusion paths.
META="$(python3 - "$TAKEOVER_RECORD_FD" "$CANON_STORE" "$RUN_ID" <<'PY'
import hashlib,json,os,stat,sys
fd,store,rid=sys.argv[1:]
try:
 fd=int(fd); st=os.fstat(fd)
 if not stat.S_ISREG(st.st_mode) or st.st_size>1024*1024 or st.st_uid!=os.getuid(): raise ValueError()
 os.lseek(fd,0,os.SEEK_SET)
 # Full-read loop to the exact fstat size: short data or growth past the
 # opened size is never accepted as the record bytes (01-VERIFICATION gap).
 chunks=[]; total=0
 while total<st.st_size:
  c=os.read(fd,min(65536,st.st_size-total))
  if not c: raise ValueError()
  chunks.append(c); total+=len(c)
 if os.read(fd,1): raise ValueError()
 raw=b''.join(chunks)
 d=json.loads(raw.decode('utf-8'))
 if not isinstance(d,dict) or d.get('ids',{}).get('run_id')!=rid: raise ValueError()
 if d.get('gates_store')!=store or d.get('gates_store_anchor')!=hashlib.sha256(store.encode()).hexdigest(): raise RuntimeError('decoy')
 for k in ('grants','forbid','unresolved_findings'):
  if not isinstance(d.get(k),list): raise ValueError()
 if not isinstance(d.get('resume'),dict) or not isinstance(d['resume'].get('preconditions'),list): raise ValueError()
 git_state=d.get('git_state',{})
 if not isinstance(git_state,dict) or not isinstance(git_state.get('dirty'),list): raise ValueError()
 if not all(isinstance(x,str) for x in git_state['dirty']): raise ValueError()
 # Hostile forbid/precondition strings are capped and rendered through the
 # same C0/C1 inert map used for rid/branch/command display fields BEFORE
 # they are compared or reach refusal tokens (01-VERIFICATION gap).
 def _inert(s): return ''.join(' ' if ord(ch)<32 or 127<=ord(ch)<=159 else ch for ch in s)[:128]
 def _scrub(rows):
  out=[]
  for row in rows:
   if isinstance(row,dict):
    row={k:(_inert(v) if k in ('action','probe','reason') and isinstance(v,str) else v) for k,v in row.items()}
   elif isinstance(row,str):
    row=_inert(row)
   out.append(row)
  return out
 d['forbid']=_scrub(d['forbid'])
 d['resume']['preconditions']=_scrub(d['resume']['preconditions'])
 print(json.dumps({'created_at':d.get('created_at'),'git':git_state,'grants':d['grants'],'forbid':d['forbid'],'preflight':d.get('preflight',{}),'findings':d['unresolved_findings'],'pre':d['resume']['preconditions']},separators=(',',':')))
except RuntimeError: print('DECOY')
except Exception: print('BAD')
PY
)"
[ "$META" = DECOY ] && refuse decoy-store
[ "$META" != BAD ] || refuse decoy-store

record_entry_matches() {
  python3 - "$TAKEOVER_DIR_FD" "$TAKEOVER_RECORD_NAME" "$TAKEOVER_RECORD_FD" <<'PY'
import os,sys
try:
 entry=os.stat(sys.argv[2],dir_fd=int(sys.argv[1]),follow_symlinks=False); opened=os.fstat(int(sys.argv[3]))
 raise SystemExit(0 if (entry.st_dev,entry.st_ino)==(opened.st_dev,opened.st_ino) else 1)
except OSError: raise SystemExit(1)
PY
}

# Owner ownership is acquired by the descriptor bootstrap through the
# serialized, crash-released OwnerLock; a record path without the held-lock
# marker is a bootstrap fault, never a success path.
[ "${TAKEOVER_LOCK_HELD:-}" = 1 ] || refuse record-mismatch
cleanup() {
  python3 - "$TAKEOVER_STORE_DIR_FD" "$TAKEOVER_LOCK_DEV" "$TAKEOVER_LOCK_INO" <<'PY'
import os,sys
try:
 st=os.stat('.takeover-check.lock',dir_fd=int(sys.argv[1]),follow_symlinks=False)
 if (st.st_dev,st.st_ino)==(int(sys.argv[2]),int(sys.argv[3])): os.unlink('.takeover-check.lock',dir_fd=int(sys.argv[1]))
except OSError: pass
PY
}
trap cleanup EXIT
# A delivered signal must stop the wall after lock cleanup, never fall through
# into continued policy execution: exit fires the EXIT trap (cleanup) and the
# process terminates with the conventional 128+signum (01-VERIFICATION gap).
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Rebase state comes exclusively from Git-resolved administrative paths so
# ordinary and linked worktrees are both exact; query errors fail closed.
REBASE_MERGE="$(git rev-parse --git-path rebase-merge 2>/dev/null)" || refuse record-mismatch
REBASE_APPLY="$(git rev-parse --git-path rebase-apply 2>/dev/null)" || refuse record-mismatch
[ ! -d "$REBASE_MERGE" ] && [ ! -d "$REBASE_APPLY" ] || refuse mid-rebase
branch="$(git branch --show-current 2>/dev/null)" || refuse record-mismatch
head="$(git rev-parse "HEAD^{commit}" 2>/dev/null)" || refuse record-mismatch
# The exact recorded full HEAD is always compared, including a recorded
# detached state (empty branch), which still pins the exact commit.
if ! python3 - "$META" "$branch" "$head" <<'PY'
import json,sys
g=json.loads(sys.argv[1])['git']
recorded_branch=g.get('branch') or ''
recorded_head=g.get('head') or ''
current_branch,current_head=sys.argv[2],sys.argv[3]
if recorded_branch:
    ok = current_branch == recorded_branch and current_head == recorded_head
else:
    ok = current_branch == '' and current_head == recorded_head
raise SystemExit(0 if ok else 1)
PY
then refuse branch-gone; fi
recorded_branch="$(python3 - "$META" <<'PY'
import json,sys; print(json.loads(sys.argv[1])['git'].get('branch') or '')
PY
)"
# A named recorded branch and upstream validate against local refs only; the
# wall never fetches (EDGE-002).
if [ -n "$recorded_branch" ]; then
  git rev-parse --verify --quiet "refs/heads/$recorded_branch" >/dev/null 2>&1 || refuse branch-gone
fi
upstream="$(python3 - "$META" <<'PY'
import json,sys; print(json.loads(sys.argv[1])['git'].get('upstream',''))
PY
)"
[ -z "$upstream" ] || git rev-parse --verify "$upstream" >/dev/null 2>&1 || refuse branch-gone

# A bound record unlocks one and only one live-state read. All authority facts
# below are taken from this JSON snapshot; no later check reopens the ledger.
record_entry_matches || refuse record-mismatch
# The resume-action set is the wall's fixed typed class table ({ship:gsd})
# unioned with validated additive record grant rows; the pure evaluator must
# return a verdict for every one of them.
record_grant_actions="$(python3 - "$META" <<'PY'
import json,sys
rows=json.loads(sys.argv[1]).get('grants',[])
names=sorted({r.get('action') for r in rows
              if isinstance(r,dict) and isinstance(r.get('action'),str) and r.get('action')})
print('\n'.join(names))
PY
)" || refuse record-mismatch
eval_actions=(--action ship:gsd)
while IFS= read -r action; do
  [ -n "$action" ] && eval_actions+=(--action "$action")
done <<< "$record_grant_actions"
evaluation="$(evaluate "${eval_actions[@]}" 2>/dev/null)" || refuse record-mismatch
state="$(python3 - "$evaluation" <<'PY'
import json,sys
d=json.loads(sys.argv[1])
assert isinstance(d.get('state'),dict)
print(json.dumps(d['state'],separators=(',',':')))
PY
)" || refuse record-mismatch
clock_and_wip="$(TAKEOVER_NOW="${TAKEOVER_NOW:-}" python3 - "$META" "$state" 2>&1 <<'PY'
import hashlib,json,os,subprocess,sys,time
meta,state=json.loads(sys.argv[1]),json.loads(sys.argv[2])
now_raw=os.environ.get('TAKEOVER_NOW','')
try: now=int(now_raw) if now_raw else int(time.time())
except ValueError: raise SystemExit('record-mismatch')
created=state.get('takeover_created_at')
digest=state.get('takeover_dirty_digest')
record_created=meta.get('created_at')
try:
    if isinstance(created,bool) or not isinstance(created,(int,float)) or int(created) > now: raise ValueError
    created=int(created)
except (TypeError,ValueError): raise SystemExit('record-mismatch')
if record_created != created: raise SystemExit('record-created')
if not isinstance(digest,str) or len(digest) != 64: raise SystemExit('record-digest-shape')
status=subprocess.run(['git','status','--porcelain=v2','-z','--untracked-files=all'],capture_output=True)
if status.returncode: raise SystemExit('dirty-worktree')
rows=status.stdout.split(b'\0')
kept=[]
for row in rows:
    if not row: continue
    # The writer stores the exact NUL records.  Exclude its own bookkeeping by
    # path on both producer and consumer, including both rename paths.
    path = row[2:] if row.startswith(b'? ') else row.rsplit(b' ',1)[-1]
    if path.startswith(b'.feature-fix-swarm/') or path == b'.feature-fix-swarm': continue
    kept.append(row)
kept.sort()
live_digest=hashlib.sha256(b'\0'.join(kept)).hexdigest()
record_dirty=meta['git']['dirty']
try: record_digest=hashlib.sha256('\0'.join(sorted(record_dirty)).encode('utf-8','surrogateescape')).hexdigest()
except UnicodeEncodeError: raise SystemExit('record-mismatch')
if record_digest != digest: raise SystemExit('record-digest')
if live_digest != digest: raise SystemExit('dirty-worktree')
# EDGE-001 precision backstops: a forged future-leaning created_at cannot buy
# freshness past the record artifact mtime or the ledger grant anchors.
anchors=[created]
try: anchors.append(int(os.fstat(int(os.environ['TAKEOVER_RECORD_FD'])).st_mtime))
except (KeyError,ValueError,OSError): raise SystemExit('record-mismatch')
for row in state.get('grants',[]):
    if isinstance(row,dict):
        granted=row.get('granted_at')
        if isinstance(granted,(int,float)) and not isinstance(granted,bool):
            anchors.append(int(granted))
if now-min(anchors) >= 259200: raise SystemExit('stale-record')
print('ok')
PY
)" || case "$clock_and_wip" in
  *dirty-worktree*) refuse dirty-worktree ;;
  *stale-record*) refuse grant-expired stale-record ;;
  *) refuse record-mismatch ;;
esac

# This floor is intentionally record-independent. Empty record collections
# cannot select a check off; record rows can only add constraints below.
python3 - "$evaluation" <<'PY' >/dev/null
import json,sys
d=json.loads(sys.argv[1]); raise SystemExit(0 if d.get('preflight_ok') is True else 1)
PY
if [ "$?" -ne 0 ]; then refuse preflight-stale; fi
required_actions="$(python3 - "$META" "$state" <<'PY'
import json,sys
meta,state=map(json.loads,sys.argv[1:])
live={r.get('action'):r for r in state.get('grants',[]) if isinstance(r,dict) and isinstance(r.get('action'),str)}
record={r.get('action'):r for r in meta.get('grants',[]) if isinstance(r,dict) and isinstance(r.get('action'),str)}
if set(record) != set(live): raise SystemExit('record-mismatch')
for action,row in record.items():
    live_row=live[action]
    if row.get('granted_at') != live_row.get('granted_at') or row.get('expires_at') != live_row.get('expires_at'):
        raise SystemExit('record-mismatch')
actions={'ship:gsd'} | set(live)
print('\n'.join(sorted(actions)))
PY
)" || refuse record-mismatch
while IFS= read -r action; do
  [ -n "$action" ] || continue
  # A missing verdict is an evaluator/schema fault, not a denial: refuse it
  # as record-mismatch so it can never be retried as a grant remedy.
  python3 - "$evaluation" "$action" <<'PY' >/dev/null
import json,sys
results=json.loads(sys.argv[1]).get('grant_results',{})
if sys.argv[2] not in results: raise SystemExit(2)
raise SystemExit(0 if results[sys.argv[2]] is True else 1)
PY
  case "$?" in
    0) ;;
    2) refuse record-mismatch ;;
    *) refuse grant-expired "$action" ;;
  esac
done <<< "$required_actions"
if python3 - "$evaluation" <<'PY'
import json,sys
try: raise SystemExit(0 if json.loads(sys.argv[1]).get('unresolved_findings') else 1)
except Exception: raise SystemExit(1)
PY
then refuse findings-open; fi
poison_actions="$(python3 - "$META" <<'PY'
import json,sys
d=json.loads(sys.argv[1])
rows=d['forbid']+d['pre']
actions=[]
for row in rows:
  if isinstance(row,dict) and isinstance(row.get('kind'),str) and isinstance(row.get('value'),str) and 'action' not in row:
    # Producer identity preconditions are display-only context for a cold
    # session, not deterministic action prohibitions.
    continue
  action=row.get('action') if isinstance(row,dict) else row
  if not isinstance(action,str) or not action: raise SystemExit(1)
  actions.append(action)
print('\n'.join(sorted(set(actions))))
PY
)" || refuse record-mismatch
while IFS= read -r action; do [ -z "$action" ] || refuse "poison/$action"; done <<< "$poison_actions"

# The success transaction is one bounded descriptor-relative gates.py command
# sharing the canonical evidence.lock with every ordinary _StoreLock writer:
# one post-decision read, a full policy re-gate under the lock, a durable
# fsynced intent, exact record consume, and field-preserving expectation
# clearing — TAKEOVER-OK is emitted before the intent is deleted.
if [ -n "${TAKEOVER_TEST_PAUSE_BEFORE_CONSUME:-}" ]; then
  : > "$TAKEOVER_TEST_PAUSE_BEFORE_CONSUME/ready"
  for _ in $(seq 1 600); do
    [ -e "$TAKEOVER_TEST_PAUSE_BEFORE_CONSUME/release" ] && break
    sleep 0.05
  done
fi
[ -n "${TAKEOVER_STORE_DIR_FD:-}" ] && [ -n "${TAKEOVER_EVIDENCE_FD:-}" ] || refuse record-mismatch
consume_out="$(python3 "$GP" takeover-consume "$RUN_ID" --consumed-at "$(date +%s)" \
  --store-dir-fd "$TAKEOVER_STORE_DIR_FD" --store-fd "$TAKEOVER_EVIDENCE_FD" \
  --takeover-dir-fd "$TAKEOVER_DIR_FD" --record-fd "$TAKEOVER_RECORD_FD" \
  --record-name "$TAKEOVER_RECORD_NAME" --snapshot-sha256 "$TAKEOVER_SNAPSHOT_SHA256" \
  --deadline-ms 1000)" || {
  case "$consume_out" in
    REFUSED:*) refuse "${consume_out#REFUSED:}" ;;
    *) refuse record-mismatch ;;
  esac
}
if [ "$MODE" = json ]; then
  python3 - "$RUN_ID" <<'PY'
import json,sys
print(json.dumps({'schema_version':1,'run_id':sys.argv[1],'verdict':'TAKEOVER-OK','reason':None,'remedy':None},separators=(',',':')))
PY
else
  printf '%s\n' "$consume_out"
fi
