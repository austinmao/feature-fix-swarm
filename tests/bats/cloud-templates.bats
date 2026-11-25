#!/usr/bin/env bats
# templates/cloud/{setup,env-check,session-start}.sh and config.json.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  T="$ROOT/templates/cloud"
  W="$BATS_TEST_TMPDIR/w"
  H="$BATS_TEST_TMPDIR/home"
  STUB="$BATS_TEST_TMPDIR/stub"
  mkdir -p "$W/.ffs/cloud" "$H" "$STUB"
  cp "$T"/*.sh "$T/config.json" "$W/.ffs/cloud/"
  unset CLAUDE_PROJECT_DIR
  git -C "$W" init -q -b main
  git -C "$W" -c user.email=a@b -c user.name=n commit -q --allow-empty -m init
}

mkstub() { printf '#!/bin/sh\n%s\n' "$2" >"$STUB/$1"; chmod +x "$STUB/$1"; }

# ---- config.json ----
@test "config: defaults and exposure shape" {
  run python3 -c 'import json,sys
c=json.load(open(sys.argv[1]))
assert c["secrets"]["provider"]=="none"
assert c["secrets"]["exposure"]=={"claude":[],"codex":[]}
assert "names" not in c["secrets"]
assert c["scan"]=={"allow":[]} and c["verify"]["cmd"]==""
assert "review" not in c
assert c["deadline_hours"]==12 and c["schema"]==1' "$T/config.json"
  [ "$status" -eq 0 ]
}

# ---- setup.sh ----
@test "setup: re-exec scrubs canary env from installers" {
  : >"$W/package-lock.json"
  mkstub npm 'echo "${FFS_CANARY:-unset}" >"$HOME/npm-saw"'
  run env PATH="$STUB:$PATH" HOME="$H" FFS_CANARY=s3cret bash "$W/.ffs/cloud/setup.sh"
  [ "$status" -eq 0 ]
  [ "$(cat "$H/npm-saw")" = "unset" ]
  ! grep -rq s3cret "$H"
}

@test "setup: stamp makes second run a no-op; status JSON valid" {
  : >"$W/package-lock.json"
  mkstub npm '[ "$1" = ci ] && echo x >>"$HOME/npm-count"; exit 0'
  run env PATH="$STUB:$PATH" HOME="$H" bash "$W/.ffs/cloud/setup.sh"
  run env PATH="$STUB:$PATH" HOME="$H" bash "$W/.ffs/cloud/setup.sh"
  [ "$status" -eq 0 ]
  [ "$(cat "$H/npm-count")" = "x" ]
  ls "$H"/.ffs-cloud-setup.* >/dev/null
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["steps"]["npm-ci"]=="ok"
assert d["secret_isolation"]=="env-i-only"' "$H/.ffs-cloud-env-status"
}

@test "setup: header states the residual /proc secret-visibility limit" {
  grep -q 'SECRET ISOLATION LIMIT' "$T/setup.sh"
  grep -q '/proc/<pid>/environ' "$T/setup.sh"
}

@test "setup: failed install is recorded, script continues, no stamp" {
  : >"$W/package-lock.json"; : >"$W/uv.lock"
  mkstub npm 'exit 1'
  mkstub uv 'echo ok >"$HOME/uv-ran"'
  run env PATH="$STUB:$PATH" HOME="$H" bash "$W/.ffs/cloud/setup.sh"
  [ "$status" -eq 0 ]
  [ -f "$H/uv-ran" ]
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["steps"]["npm-ci"]=="failed" and d["ok"] is False' "$H/.ffs-cloud-env-status"
  ! ls "$H"/.ffs-cloud-setup.* 2>/dev/null
}

@test "setup: doppler placeholder pins refuse install" {
  sed -i.bak 's/"provider": "none"/"provider": "doppler"/' "$W/.ffs/cloud/config.json"
  mkstub curl 'echo called >"$HOME/curl-ran"'
  # minimal PATH so a host-installed doppler cannot short-circuit the pin check
  run env PATH="$STUB:/usr/bin:/bin" HOME="$H" bash "$W/.ffs/cloud/setup.sh"
  [ "$status" -eq 0 ]
  [ ! -f "$H/curl-ran" ]
  grep -q '"doppler":"unpinned"' "$H/.ffs-cloud-env-status"
}

@test "setup: provider command runs install only when non-empty" {
  python3 - "$W/.ffs/cloud/config.json" <<'PY'
import json,sys
p=sys.argv[1]; c=json.load(open(p))
c["secrets"]["provider"]="command"; c["secrets"]["install"]="touch \"$HOME/installed\""
json.dump(c,open(p,"w"))
PY
  run env HOME="$H" PATH="$PATH" bash "$W/.ffs/cloud/setup.sh"
  [ "$status" -eq 0 ]
  [ -f "$H/installed" ]
}

# ---- env-check.sh ----
@test "env-check: present/missing by name, never a value" {
  python3 - "$W/.ffs/cloud/config.json" <<'PY'
import json,sys
p=sys.argv[1]; c=json.load(open(p))
c["secrets"]["exposure"]["claude"]=["FFS_HAS","FFS_NOT","host:api.example.com"]
json.dump(c,open(p,"w"))
PY
  printf '{"schema":1,"ok":true}\n' >"$H/.ffs-cloud-env-status"
  run env HOME="$H" FFS_HAS=canary-value-123 bash "$W/.ffs/cloud/env-check.sh" claude
  [ "$status" -eq 0 ]
  [[ "$output" != *canary-value-123* ]]
  python3 -c 'import json,sys; d=json.loads(sys.argv[1])
assert set(d)=={"tools","present","missing"}, sorted(d)
assert d["present"]==["FFS_HAS"] and d["missing"]==["FFS_NOT"] and "git" in d["tools"]' "$output"
}

@test "env-check: prints exactly the env_report keys, with or without a status file" {
  run env HOME="$H" bash "$W/.ffs/cloud/env-check.sh" codex
  [ "$status" -eq 0 ]
  python3 -c 'import json,sys; assert set(json.loads(sys.argv[1]))=={"tools","present","missing"}' "$output"
  printf '{"schema":1,"ok":true}\n' >"$H/.ffs-cloud-env-status"
  run env HOME="$H" bash "$W/.ffs/cloud/env-check.sh" codex
  python3 -c 'import json,sys; assert set(json.loads(sys.argv[1]))=={"tools","present","missing"}' "$output"
}

@test "env-check: tool versions are single validator-safe tokens, not banners" {
  mkstub git 'echo "git version 2.50.1 (Apple Git-155)"'
  mkstub node 'echo v20.11.0'
  mkstub jq 'echo jq-1.7.1'
  mkstub bats 'echo "Bats 1.10.0"'
  mkstub npm 'echo "no digits here"'
  run env HOME="$H" PATH="$STUB:$PATH" bash "$W/.ffs/cloud/env-check.sh" claude
  [ "$status" -eq 0 ]
  python3 -c 'import json,re,sys
t=json.loads(sys.argv[1])["tools"]
assert t["git"]=="2.50.1" and t["node"]=="20.11.0" and t["jq"]=="1.7.1" and t["bats"]=="1.10.0" and t["npm"]=="unknown", t
assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~:-]{0,63}", v) for v in t.values()), t' "$output"
}

@test "env-check: --status prints the setup status or null; bad arguments exit 2" {
  run env HOME="$H" bash "$W/.ffs/cloud/env-check.sh" codex --status
  [ "$status" -eq 0 ]; [ "$output" = "null" ]
  printf '{"schema":1,"ok":true}\n' >"$H/.ffs-cloud-env-status"
  run env HOME="$H" bash "$W/.ffs/cloud/env-check.sh" codex --status
  [ "$output" = '{"schema":1,"ok":true}' ]
  run env HOME="$H" bash "$W/.ffs/cloud/env-check.sh" bogus
  [ "$status" -eq 2 ]
  run env HOME="$H" bash "$W/.ffs/cloud/env-check.sh" codex --bogus
  [ "$status" -eq 2 ]
}

# ---- session-start.sh ----
ss_stub() { printf '#!/bin/bash\n%s\n' "$1" >"$W/.ffs/cloud/setup.sh"; }
ss_run() { run env HOME="$H" "$@" bash "$W/.ffs/cloud/session-start.sh"; }

@test "session-start: local session does nothing" {
  ss_stub 'touch "$HOME/ran"'
  git -C "$W" checkout -q -b ffs/cloud/R1/T1-g1
  ss_run CLAUDE_CODE_REMOTE=false
  [ "$status" -eq 0 ]; [ -z "$output" ]; [ ! -f "$H/ran" ]
}
@test "session-start: remote ordinary branch does nothing" {
  ss_stub 'touch "$HOME/ran"'
  ss_run CLAUDE_CODE_REMOTE=true
  [ "$status" -eq 0 ]; [ -z "$output" ]; [ ! -f "$H/ran" ]
}
@test "session-start: remote ffs/cloud branch runs setup" {
  ss_stub 'touch "$HOME/ran"'
  git -C "$W" checkout -q -b ffs/cloud/R1/T1-g1
  ss_run CLAUDE_CODE_REMOTE=true
  [ "$status" -eq 0 ]; [ -z "$output" ]; [ -f "$H/ran" ]
}
@test "session-start: remote with FFS_CLOUD_PROVISION=1 runs setup" {
  ss_stub 'touch "$HOME/ran"'
  ss_run CLAUDE_CODE_REMOTE=true FFS_CLOUD_PROVISION=1
  [ "$status" -eq 0 ]; [ -f "$H/ran" ]
}
@test "session-start: detached HEAD is not a branch match" {
  ss_stub 'touch "$HOME/ran"'
  git -C "$W" checkout -q --detach
  ss_run CLAUDE_CODE_REMOTE=true
  [ "$status" -eq 0 ]; [ ! -f "$H/ran" ]
}
@test "session-start: setup failure still exits 0, silent" {
  ss_stub 'echo SECRETISH; exit 7'
  ss_run CLAUDE_CODE_REMOTE=true FFS_CLOUD_PROVISION=1
  [ "$status" -eq 0 ]; [ -z "$output" ]
}
@test "session-start: setup hang is killed at the cap" {
  ss_stub 'sleep 30'
  local s=$SECONDS
  ss_run CLAUDE_CODE_REMOTE=true FFS_CLOUD_PROVISION=1 FFS_CLOUD_SETUP_TIMEOUT=1
  [ "$status" -eq 0 ]
  [ $((SECONDS - s)) -lt 15 ]
}

@test "session-start: finds the repo root from a subdirectory, not from the working directory" {
  ss_stub 'touch "$HOME/ran"'
  mkdir -p "$W/pkg/deep"
  git -C "$W" checkout -q -b ffs/cloud/R1/T1-g1
  cd "$W/pkg/deep"
  ss_run CLAUDE_CODE_REMOTE=true
  [ "$status" -eq 0 ]; [ -f "$H/ran" ]
}

@test "session-start: CLAUDE_PROJECT_DIR wins and the root's setup.sh runs, not a planted copy" {
  mkdir -p "$W/pkg/.ffs/cloud"
  cp "$W/.ffs/cloud/session-start.sh" "$W/pkg/.ffs/cloud/session-start.sh"
  printf '#!/bin/bash\ntouch "$HOME/planted"\n' >"$W/pkg/.ffs/cloud/setup.sh"
  ss_stub 'touch "$HOME/root-ran"'
  run env HOME="$H" CLAUDE_CODE_REMOTE=true FFS_CLOUD_PROVISION=1 CLAUDE_PROJECT_DIR="$W" \
    bash "$W/pkg/.ffs/cloud/session-start.sh"
  [ "$status" -eq 0 ]
  [ -f "$H/root-ran" ]; [ ! -f "$H/planted" ]
}
