#!/usr/bin/env bash
# Deterministic takeover wall.  Records are hostile discovery data, never authority.
set -uo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$SCRIPT_ROOT")"
GP="$SCRIPT_ROOT/lib/gates.py"
RUN_ID="${GSD_RUN_ID:-}"; MODE=text; LIST=0; LOCK=""
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
STORE_DIR="$(gate store-dir 2>/dev/null)" || refuse decoy-store
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
  expectation="$(gate takeover-expectation "$RUN_ID" 2>/dev/null || true)"
  [[ "$expectation" == *'"takeover_expected": true'* ]]
}
none_or_missing() {
  if expectation_present; then
    refuse missing-record
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
 os.lseek(fd,0,os.SEEK_SET); raw=os.read(fd,1024*1024+1)
 if len(raw)>1024*1024: raise ValueError()
 d=json.loads(raw.decode('utf-8'))
 if not isinstance(d,dict) or d.get('ids',{}).get('run_id')!=rid: raise ValueError()
 if d.get('gates_store')!=store or d.get('gates_store_anchor')!=hashlib.sha256(store.encode()).hexdigest(): raise RuntimeError('decoy')
 for k in ('grants','forbid','unresolved_findings'):
  if not isinstance(d.get(k),list): raise ValueError()
 if not isinstance(d.get('resume'),dict) or not isinstance(d['resume'].get('preconditions'),list): raise ValueError()
 git_state=d.get('git_state',{})
 if not isinstance(git_state,dict) or not isinstance(git_state.get('dirty'),list): raise ValueError()
 if not all(isinstance(x,str) for x in git_state['dirty']): raise ValueError()
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

# O_EXCL ownership is held through all subsequent checks and record consume.
LOCK="$STORE_DIR/.takeover-check.lock"
if [ "${TAKEOVER_LOCK_HELD:-}" != 1 ] && ! python3 - "$LOCK" "$RUN_ID" <<'PY'
import json,os,sys,time
p,r=sys.argv[1:]
boot=open('/proc/sys/kernel/random/boot_id').read().strip() if os.path.exists('/proc/sys/kernel/random/boot_id') else 'unknown'
payload={'pid':os.getpid(),'pid_start_time':str(int(time.time())),'boot_session_id':boot,'claimed_at':int(time.time()),'run_id':r}
for _ in range(2):
 try:
  fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  os.write(fd,json.dumps(payload).encode()); os.close(fd); break
 except FileExistsError:
  try:
   old=json.load(open(p)); pid=int(old.get('pid',0)); stale=(old.get('boot_session_id') != boot)
   if not stale:
    try: os.kill(pid,0)
    except OSError: stale=True
   if not stale: raise SystemExit(1)
   tomb=p+'.tombstone.%s.%s'%(os.getpid(),time.time_ns())
   os.replace(p,tomb); os.unlink(tomb)
  except FileNotFoundError: continue
  except (ValueError,TypeError,json.JSONDecodeError): raise SystemExit(1)
else: raise SystemExit(1)
PY
then refuse runner-live; fi
cleanup() {
  if [ "${TAKEOVER_LOCK_HELD:-}" = 1 ]; then
    python3 - "$TAKEOVER_STORE_DIR_FD" "$TAKEOVER_LOCK_DEV" "$TAKEOVER_LOCK_INO" <<'PY'
import os,sys
try:
 st=os.stat('.takeover-check.lock',dir_fd=int(sys.argv[1]),follow_symlinks=False)
 if (st.st_dev,st.st_ino)==(int(sys.argv[2]),int(sys.argv[3])): os.unlink('.takeover-check.lock',dir_fd=int(sys.argv[1]))
except OSError: pass
PY
  else
    [ -n "$LOCK" ] && rm -f "$LOCK"
  fi
}
trap cleanup EXIT HUP INT TERM

[ ! -d "$ROOT/.git/rebase-merge" ] && [ ! -d "$ROOT/.git/rebase-apply" ] || refuse mid-rebase
branch="$(git branch --show-current 2>/dev/null || true)"; head="$(git rev-parse HEAD 2>/dev/null || true)"
if ! python3 - "$META" "$branch" "$head" <<'PY'
import json,sys
d=json.loads(sys.argv[1]); g=d['git']; raise SystemExit(0 if (not g.get('branch') or (g.get('branch')==sys.argv[2] and g.get('head')==sys.argv[3])) else 1)
PY
then refuse branch-gone; fi
upstream="$(python3 - "$META" <<'PY'
import json,sys; print(json.loads(sys.argv[1])['git'].get('upstream',''))
PY
)"
[ -z "$upstream" ] || git rev-parse --verify "$upstream" >/dev/null 2>&1 || refuse branch-gone

# A bound record unlocks one and only one live-state read. All authority facts
# below are taken from this JSON snapshot; no later check reopens the ledger.
record_entry_matches || refuse record-mismatch
state="$(gate takeover-state "$RUN_ID" 2>/dev/null)" || refuse preflight-stale
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
if now-created >= 259200: raise SystemExit('stale-record')
print('ok')
PY
)" || case "$clock_and_wip" in
  *dirty-worktree*) refuse dirty-worktree ;;
  *stale-record*) refuse grant-expired stale-record ;;
  *) refuse record-mismatch ;;
esac

# This floor is intentionally record-independent. Empty record collections
# cannot select a check off; record rows can only add constraints below.
gate check-preflight "$RUN_ID" >/dev/null 2>&1 || refuse preflight-stale
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
  gate check-grant "$RUN_ID" --action "$action" >/dev/null 2>&1 || refuse grant-expired "$action"
done <<< "$required_actions"
if python3 - "$state" <<'PY'
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

# Atomic consume under the held lock makes a second normal contender observe
# missing-record rather than a second success.
python3 "$SCRIPT_ROOT/scripts/gsd/takeover-io.py" consume \
  --takeover-fd "$TAKEOVER_DIR_FD" --record-fd "$TAKEOVER_RECORD_FD" \
  --name "$TAKEOVER_RECORD_NAME" >/dev/null || refuse record-mismatch
if [ "$MODE" = json ]; then
  python3 - "$RUN_ID" <<'PY'
import json,sys
print(json.dumps({'schema_version':1,'run_id':sys.argv[1],'verdict':'TAKEOVER-OK','reason':None,'remedy':None},separators=(',',':')))
PY
else
  echo TAKEOVER-OK
fi
