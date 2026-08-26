#!/usr/bin/env bash
# Deterministic takeover wall.  Records are hostile discovery data, never authority.
set -uo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$SCRIPT_ROOT")"
GP="$SCRIPT_ROOT/lib/gates.py"
RUN_ID="${GSD_RUN_ID:-}"; MODE=text; LIST=0; LOCK=""
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

# Only these two identity accessors are allowed to see the authoritative
# resolver before a record has proved its store binding.  In particular,
# inherited GATES_STORE is deliberately absent from every wall gates call.
gate() { env -u GATES_STORE python3 "$GP" "$@"; }
sq() { python3 -c 'import shlex,sys; print(shlex.quote(sys.argv[1]))' "$1"; }
remedy_for() {
  case "$1" in
    env-mismatch) printf 'unset GATES_STORE' ;; decoy-store|missing-record|record-mismatch) printf '/spec-status' ;;
    runner-live) printf 'bash scripts/gsd/takeover-check.sh --run-id %s' "$(sq "$RUN_ID")" ;;
    mid-rebase) printf 'git rebase --continue' ;; branch-gone) printf 'git fetch --prune origin' ;;
    dirty-worktree) printf '/adopt-wip' ;; preflight-stale) printf '/preflight' ;;
    grant-expired) printf 'python3 %s grant %s --action %s' "$(sq "$GP")" "$(sq "$RUN_ID")" "$(sq "${2:-ship:gsd}")" ;;
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
if [ -n "${GATES_STORE:-}" ]; then
  inherited="$(python3 "$GP" store-path 2>/dev/null || true)"
  [ "$inherited" = "$CANON_STORE" ] || refuse env-mismatch
fi
DIR="$STORE_DIR/takeover"
if [ "$LIST" -eq 1 ]; then
  [ -d "$DIR" ] && [ ! -L "$DIR" ] || exit 0
  python3 - "$DIR" <<'PY'
import glob,json,os,sys,time
for path in sorted(glob.glob(os.path.join(sys.argv[1],'spec-*.json'))):
 try:
  if os.path.islink(path) or not os.path.isfile(path): continue
  d=json.load(open(path)); print('%s\t%s\t%s\t%s'%(d['ids']['run_id'],int(time.time()-d['created_at']),d['git_state'].get('branch',''),d.get('resume',{}).get('command','').replace('\n',' ').replace('\r',' ')))
 except (OSError,ValueError,KeyError,TypeError): pass
PY
  exit 0
fi
if [ ! -d "$DIR" ] || [ -L "$DIR" ]; then
  state="$(gate takeover-state "$RUN_ID" 2>/dev/null || true)"
  [[ "$state" == *'"takeover_expected": true'* ]] && refuse missing-record
  if [ "$MODE" = json ]; then
    python3 - "$RUN_ID" <<'PY'
import json,sys
print(json.dumps({'schema_version':1,'run_id':sys.argv[1],'verdict':'TAKEOVER-NONE','reason':None,'remedy':None},separators=(',',':')))
PY
  else
    echo TAKEOVER-NONE
  fi
  exit 0
fi
RECORD="$DIR/$RUN_ID.json"
if [ ! -e "$RECORD" ]; then
  state="$(gate takeover-state "$RUN_ID" 2>/dev/null || true)"
  [[ "$state" == *'"takeover_expected": true'* ]] && refuse missing-record
  echo TAKEOVER-NONE; exit 0
fi

# Validate exactly the bytes opened once. O_NONBLOCK makes FIFO/device input
# bounded; O_NOFOLLOW and fstat close both symlink and type confusion paths.
META="$(python3 - "$RECORD" "$CANON_STORE" "$RUN_ID" <<'PY'
import hashlib,json,os,stat,sys
p,store,rid=sys.argv[1:]
try:
 fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
 try:
  st=os.fstat(fd)
  if not stat.S_ISREG(st.st_mode) or st.st_size>1024*1024 or st.st_uid!=os.getuid(): raise ValueError()
  raw=os.read(fd,1024*1024+1)
 finally: os.close(fd)
 if len(raw)>1024*1024: raise ValueError()
 d=json.loads(raw.decode('utf-8'))
 if not isinstance(d,dict) or d.get('ids',{}).get('run_id')!=rid: raise ValueError()
 if d.get('gates_store')!=store or d.get('gates_store_anchor')!=hashlib.sha256(store.encode()).hexdigest(): raise RuntimeError('decoy')
 for k in ('grants','forbid','unresolved_findings'):
  if not isinstance(d.get(k),list): raise ValueError()
 if not isinstance(d.get('resume'),dict) or not isinstance(d['resume'].get('preconditions'),list): raise ValueError()
 print(json.dumps({'created_at':d.get('created_at'),'mtime':st.st_mtime,'git':d.get('git_state',{}),'grants':d['grants'],'forbid':d['forbid'],'preflight':d.get('preflight',{}),'findings':d['unresolved_findings'],'pre':d['resume']['preconditions']},separators=(',',':')))
except RuntimeError: print('DECOY')
except Exception: print('BAD')
PY
)"
[ "$META" = DECOY ] && refuse decoy-store
[ "$META" != BAD ] || refuse decoy-store

# O_EXCL ownership is held through all subsequent checks and record consume.
LOCK="$STORE_DIR/.takeover-check.lock"
if ! python3 - "$LOCK" "$RUN_ID" <<'PY'
import json,os,sys,time
p,r=sys.argv[1:]
try:
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 payload={'pid':os.getpid(),'pid_start_time':str(int(time.time())),'boot_session_id':open('/proc/sys/kernel/random/boot_id').read().strip() if os.path.exists('/proc/sys/kernel/random/boot_id') else 'unknown','claimed_at':int(time.time()),'run_id':r}
 os.write(fd,json.dumps(payload).encode()); os.close(fd)
except FileExistsError: raise SystemExit(1)
PY
then refuse runner-live; fi
cleanup() { [ -n "$LOCK" ] && rm -f "$LOCK"; }
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
dirty="$(git status --porcelain -z --untracked-files=all 2>/dev/null | python3 -c 'import sys; rows=sys.stdin.buffer.read().split(b"\0"); print("1" if any(r and not (r.startswith(b"?? .feature-fix-swarm/") or r.startswith(b"?? .feature-fix-swarm")) for r in rows) else "")')"
record_dirty="$(python3 - "$META" <<'PY'
import json,sys; print('1' if json.loads(sys.argv[1])['git'].get('dirty') else '0')
PY
)"
{ [ "$record_dirty" = 1 ] && [ -n "$dirty" ]; } || { [ "$record_dirty" = 0 ] && [ -z "$dirty" ]; } || refuse dirty-worktree

age="$(python3 - "$META" <<'PY'
import json,sys,time
d=json.loads(sys.argv[1]); c=d['created_at']; m=d['mtime'];
try: print(int(time.time()-min(float(c),float(m))))
except Exception: print(999999999)
PY
)"
[ "$age" -lt 259200 ] 2>/dev/null || refuse grant-expired
state="$(gate takeover-state "$RUN_ID" 2>/dev/null || true)"
[ -n "$state" ] || refuse preflight-stale
# Only a recorded preflight assertion triggers the live predicate; absence is
# retained for backward-compatible discovery records from the tracer.
if [ "$(python3 - "$META" <<'PY'
import json,sys; print(bool(json.loads(sys.argv[1])['preflight']))
PY
)" = True ]; then gate check-preflight "$RUN_ID" >/dev/null 2>&1 || refuse preflight-stale; fi
live_actions="$(python3 - "$state" <<'PY'
import json,sys,time
try: print('\n'.join(x.get('action','') for x in json.loads(sys.argv[1]).get('grants',[]) if isinstance(x,dict)))
except Exception: pass
PY
)"
while IFS= read -r action; do [ -z "$action" ] && continue; gate check-grant "$RUN_ID" --action "$action" >/dev/null 2>&1 || refuse grant-expired "$action"; done <<< "$live_actions"
if python3 - "$state" <<'PY'
import json,sys
try: raise SystemExit(0 if json.loads(sys.argv[1]).get('unresolved_findings') else 1)
except Exception: raise SystemExit(1)
PY
then refuse findings-open; fi
for action in $(python3 - "$META" <<'PY'
import json,sys
d=json.loads(sys.argv[1]); print('\n'.join(str(x) for x in d['forbid']+d['pre']))
PY
); do refuse "poison/$action"; done

# Atomic consume under the held lock makes a second normal contender observe
# missing-record rather than a second success.
mv "$RECORD" "$DIR/$RUN_ID.consumed.$(date +%s).json" || refuse missing-record
if [ "$MODE" = json ]; then
  python3 - "$RUN_ID" <<'PY'
import json,sys
print(json.dumps({'schema_version':1,'run_id':sys.argv[1],'verdict':'TAKEOVER-OK','reason':None,'remedy':None},separators=(',',':')))
PY
else
  echo TAKEOVER-OK
fi
