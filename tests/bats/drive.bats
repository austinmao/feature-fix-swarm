#!/usr/bin/env bats

# drive.sh against a stub gsd-run: per-phase exit-code scripts in
# $STUB_ROOT/plan-<phase>, one rc consumed per launch (missing file = rc 0).

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"; REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/.planning/run-state" "$REPO/scripts/gsd" "$REPO/scripts/coord"
  cp "$ROOT/scripts/gsd/"{drive.sh,lifecycle.sh,host-pressure.sh,reconcile.sh} "$REPO/scripts/gsd/"
  cp "$ROOT/scripts/coord/coord.py" "$REPO/scripts/coord/"
  cat > "$REPO/scripts/gsd/stub-gsd-run.sh" <<'STUB'
#!/usr/bin/env bash
phase="$2"; f="$STUB_ROOT/plan-$phase"
printf "%s\n" "${GSD_PROJECT-unset}" >> "$STUB_ROOT/projects"
printf "%s %s %s\n" "${GSD_PLANNING_SYNC-unset}" "${TMPDIR-unset}" "${GSD_EXTRA_WRITABLE_WORKTREE-unset}" >> "$STUB_ROOT/runtime2"
printf "%s %s %s %s\n" "${FFS_HOST-unset}" "${TIMEOUT-unset}" "${GSD_NETWORK_MODE-unset}" "${GSD_MODEL_REQUEST-unset}" >> "$STUB_ROOT/runtime"
rc=0; if [ -f "$f" ] && [ -s "$f" ]; then rc="$(head -1 "$f")"; tail -n +2 "$f" > "$f.n" && mv "$f.n" "$f"; fi
printf '%s resume=%s respawn=%s lifecycle=%s rc=%s\n' "$GSD_RUN_ID" "${GSD_RESUME-unset}" "${FFS_RESPAWN_MAX-unset}" "${FFS_LIFECYCLE_LAUNCH-unset}" "$rc" >> "$STUB_ROOT/launches"
if [ "$rc" = sleep ]; then touch "$STUB_ROOT/sleeping-$phase"; sleep 30; rc=$?; fi
echo "drive line rc=$rc"
exit "$rc"
STUB
  chmod +x "$REPO/scripts/gsd/"*.sh
  cd "$REPO"; git init -q -b main; git -c user.email=t -c user.name=t commit -q --allow-empty -m init
  export STUB_ROOT="$BATS_TEST_TMPDIR" FFS_DRIVE_GSD_RUN="$REPO/scripts/gsd/stub-gsd-run.sh"
  export FFS_HOST_PRESSURE_FIXTURE=ok FFS_ENV_BACKOFF_BASE_SECS=0 FFS_STEP_BACKOFF_SECS=0 FFS_DISK_MIN_MB=0
  unset GSD_RESUME GSD_RUN_ID
}

teardown() {
  for f in "$BATS_TEST_TMPDIR"/repo/.planning/run-state/drive-*.json; do
    [ -f "$f" ] || continue
    pid="$(jq -r '.pid // 0' "$f" 2>/dev/null)"; [ "$pid" -gt 0 ] 2>/dev/null && kill -9 "$pid" 2>/dev/null
  done
  pkill -f "stub-gsd-run.sh" 2>/dev/null; true
}

launches() { grep -c "$1" "$STUB_ROOT/launches"; }
step() { jq -r ".steps[$1].$2" .planning/run-state/drive-$3.json; }

@test "happy path: three phases, one launch each, lifecycle done, fresh steps carry no GSD_RESUME and inner respawn 0" {
  run bash scripts/gsd/drive.sh start --verb plan-phase --phases 1-3 --project 42 --run d
  [ "$status" -eq 0 ]
  [[ "$output" == *'DRIVE:done run=d steps=3'* ]]
  [ "$(wc -l < "$STUB_ROOT/launches" | tr -d ' ')" -eq 3 ]
  grep -q '^d-p1 resume=unset respawn=0 lifecycle=off rc=0$' "$STUB_ROOT/launches"
  [ "$(jq -r .state .planning/run-state/lifecycle-d.json)" = done ]
  [ "$(jq -r '.resume_argv|join(" ")' .planning/run-state/lifecycle-d.json)" = 'scripts/gsd/drive.sh resume --run d' ]
  run bash scripts/gsd/drive.sh status --run d
  [ "$status" -eq 0 ]; [[ "$output" == 'DRIVE:done run=d '* ]]
}

@test "fail, fail, pass: retries reuse the step run id with GSD_RESUME=1 and consume step attempts" {
  printf '1\n1\n0\n' > "$STUB_ROOT/plan-2"
  run bash scripts/gsd/drive.sh start --verb plan-phase --phases 2-2 --run ffp
  [ "$status" -eq 0 ]; [[ "$output" == *'DRIVE:done'* ]]
  [ "$(launches '^ffp-p2 ')" -eq 3 ]
  [ "$(launches '^ffp-p2 resume=1 ')" -eq 2 ]
  [ "$(step 0 attempts ffp)" = 2 ]; [ "$(step 0 state ffp)" = done ]
  [[ "$output" == *'DRIVE:step-failed phase=2 rc=1 attempt=1/5'* ]]
}

@test "three signal deaths consume zero step attempts and are logged as env-death" {
  printf '143\n137\n129\n0\n' > "$STUB_ROOT/plan-3"
  run bash scripts/gsd/drive.sh start --verb plan-phase --phases 3-3 --run ed
  [ "$status" -eq 0 ]; [[ "$output" == *'DRIVE:done'* ]]
  [ "$(step 0 attempts ed)" = 0 ]; [ "$(step 0 env_deaths ed)" = 3 ]; [ "$(step 0 launches ed)" = 4 ]
  [ "$(printf '%s\n' "$output" | grep -c 'DRIVE:env-death phase=3')" -eq 3 ]
  [[ "$output" != *'DRIVE:step-failed'* ]]
}

@test "env-death budget exhaustion quarantines the step, records pending, and the drive continues" {
  printf '143\n143\n143\n' > "$STUB_ROOT/plan-4"
  run env FFS_ENV_DEATH_MAX=2 bash scripts/gsd/drive.sh start --verb plan-phase --phases 4-5 --run edx
  [ "$status" -eq 0 ]; [[ "$output" == *'DRIVE:done run=edx steps=2'* ]]
  [ "$(step 0 state edx)" = quarantined ]; [ "$(step 1 state edx)" = done ]
  [[ "$output" == *'DRIVE:quarantined phase=4 reason="env-death-budget-exhausted'* ]]
  grep -q 'phase 4: quarantined (env-death-budget-exhausted' .planning/run-state/drive-edx.pending.md
}

@test "identical no-progress failures quarantine after FFS_NO_PROGRESS_MAX, before step attempts run out" {
  printf '5\n5\n5\n5\n5\n5\n5\n' > "$STUB_ROOT/plan-6"
  run bash scripts/gsd/drive.sh start --verb plan-phase --phases 6-7 --run np
  [ "$status" -eq 0 ]
  [ "$(step 0 state np)" = quarantined ]; [ "$(step 0 no_progress np)" = 3 ]
  [[ "$output" == *'reason="no-progress identical-failures=3 rc=5"'* ]]
  [[ "$output" == *'DRIVE:continue-next from_phase=6'* ]]
  [ "$(step 1 state np)" = done ]
  [ "$(launches '^np-p6 ')" -lt 7 ]
}

@test "step attempts exhaust at FFS_STEP_ATTEMPTS when failures differ" {
  printf '2\n3\n4\n5\n6\n7\n' > "$STUB_ROOT/plan-8"
  run env FFS_STEP_ATTEMPTS=4 bash scripts/gsd/drive.sh start --verb plan-phase --phases 8-8 --run sa
  [ "$status" -eq 0 ]
  [ "$(step 0 state sa)" = quarantined ]; [ "$(step 0 attempts sa)" = 4 ]
  [[ "$output" == *'step-attempts-exhausted attempts=4 max=4'* ]]
}

@test "rc 78 blocks the step with zero retries; execute-phase default parks as waiting(manual)" {
  printf '78\n0\n' > "$STUB_ROOT/plan-9"
  run bash scripts/gsd/drive.sh start --verb execute-phase --phases 9-10 --run blk
  [ "$status" -eq 0 ]
  [[ "$output" == *'DRIVE:blocked phase=9 reason="operator-input-required'* ]]
  [[ "$output" == *'DRIVE:parked run=blk at_phase=9'* ]]
  [ "$(launches '^blk-p9 ')" -eq 1 ]; [ ! -f "$STUB_ROOT/launches" ] || ! grep -q '^blk-p10 ' "$STUB_ROOT/launches"
  [ "$(jq -r .state .planning/run-state/drive-blk.json)" = parked ]
  [ "$(jq -r .state .planning/run-state/lifecycle-blk.json)" = waiting ]
  run bash scripts/gsd/drive.sh status --run blk
  [ "$status" -eq 0 ]; [[ "$output" == 'DRIVE:parked run=blk '*'pending=1'* ]]
}

@test "launch ceiling bounds a step across every class" {
  printf '143\n1\n143\n1\n143\n1\n' > "$STUB_ROOT/plan-11"
  run env FFS_STEP_LAUNCH_CEILING=4 FFS_NO_PROGRESS_MAX=9 bash scripts/gsd/drive.sh start --verb plan-phase --phases 11-11 --run lc
  [ "$status" -eq 0 ]
  [ "$(step 0 state lc)" = quarantined ]; [ "$(step 0 launches lc)" = 4 ]
  [[ "$output" == *'launch-ceiling launches=4 max=4'* ]]
}

@test "--after-step runs on success with {N} substituted; its failure is a step failure" {
  printf '0\n0\n' > "$STUB_ROOT/plan-12"
  run bash scripts/gsd/drive.sh start --verb plan-phase --phases 12-12 --run as --after-step "echo gate-{N} >> '$STUB_ROOT/gates'; [ \$(wc -l < '$STUB_ROOT/gates') -ge 2 ]"
  [ "$status" -eq 0 ]; [[ "$output" == *'DRIVE:after-step-failed phase=12'* ]]; [[ "$output" == *'DRIVE:done'* ]]
  [ "$(cat "$STUB_ROOT/gates")" = $'gate-12\ngate-12' ]
  [ "$(step 0 attempts as)" = 1 ]
  [[ "$output" == *"DRIVE:after-step-failed phase=12 rc=1"* ]]
}

@test "--detach runs in its own session, survives a TERM of the launcher's process group, kill -9 then reconcile resumes the same step run id" {
  printf 'sleep\n0\n' > "$STUB_ROOT/plan-13"
  run bash -c 'set -m; bash scripts/gsd/drive.sh start --verb plan-phase --phases 13-14 --run det --detach'
  [ "$status" -eq 0 ]; [[ "$output" == *'DRIVE:started run=det pid='* ]]
  pid="$(jq -r .pid .planning/run-state/drive-det.json)"
  for i in $(seq 1 250); do [ -f "$STUB_ROOT/sleeping-13" ] && break; sleep 0.02; done
  [ -f "$STUB_ROOT/sleeping-13" ]
  [ "$(ps -o pgid= -p "$pid" | tr -d ' ')" = "$pid" ]
  [ "$(ps -o pgid= -p "$pid" | tr -d ' ')" != "$(ps -o pgid= -p $$ | tr -d ' ')" ]
  run bash scripts/gsd/drive.sh status --run det
  [ "$status" -eq 0 ]; [[ "$output" == 'DRIVE:alive run=det '* ]]
  # the whole supervisor tree dies (memory reap / SIGKILL): no trap ran
  pkill -9 -f "stub-gsd-run.sh plan-phase 13" || true
  kill -9 "$pid"; sleep 0.3
  run bash scripts/gsd/drive.sh status --run det
  [ "$status" -eq 1 ]; [[ "$output" == 'DRIVE:dead run=det '* ]]
  [ "$(jq -r .state .planning/run-state/lifecycle-det.json)" = running ]
  jq '.launched_at=1' .planning/run-state/lifecycle-det.json > r && mv r .planning/run-state/lifecycle-det.json
  run env FFS_RECONCILE_STALE_SECS=1 bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:relaunched run=det'* ]]
  for i in $(seq 1 500); do [ "$(jq -r .state .planning/run-state/drive-det.json)" = done ] && break; sleep 0.02; done
  [ "$(jq -r .state .planning/run-state/drive-det.json)" = done ]
  grep -q '^det-p13 resume=unset ' "$STUB_ROOT/launches"
  grep -q '^det-p13 resume=1 ' "$STUB_ROOT/launches"
  grep -q '^det-p14 resume=unset ' "$STUB_ROOT/launches"
  [ "$(step 0 env_deaths det)" = 1 ]
  [ "$(jq -r .budgets.env_deaths .planning/run-state/lifecycle-det.json)" = 7 ]
  grep -q 'DRIVE:supervisor-death-charged phase=13' .planning/run-state/reconcile-det.log
}

@test "stop is terminal for reconcile; resume refuses a live supervisor; start refuses an existing run id" {
  printf 'sleep\n' > "$STUB_ROOT/plan-15"
  bash scripts/gsd/drive.sh start --verb plan-phase --phases 15-15 --run st --detach >/dev/null
  for i in $(seq 1 250); do [ -f "$STUB_ROOT/sleeping-15" ] && break; sleep 0.02; done
  run bash scripts/gsd/drive.sh resume --run st
  [ "$status" -eq 1 ]; [[ "$output" == *already-running* ]]
  run bash scripts/gsd/drive.sh start --verb plan-phase --phases 15-15 --run st
  [ "$status" -eq 1 ]; [[ "$output" == *'DRIVE:exists'* ]]
  run bash scripts/gsd/drive.sh stop --run st
  [ "$status" -eq 0 ]
  [ "$(jq -r .state .planning/run-state/lifecycle-st.json)" = failed ]
  [ "$(jq -r .reason .planning/run-state/lifecycle-st.json)" = operator-stop ]
  run env FFS_RECONCILE_STALE_SECS=1 bash scripts/gsd/reconcile.sh
  [[ "$output" == *'RECONCILE:terminal run=st state=failed'* ]]
}

@test "usage errors are typed" {
  run bash scripts/gsd/drive.sh start --verb nope --phases 1-2
  [ "$status" -eq 1 ]; [[ "$output" == DRIVE:usage* ]]
  run bash scripts/gsd/drive.sh start --verb plan-phase --phases 3-1
  [ "$status" -eq 1 ]
  run bash scripts/gsd/drive.sh resume
  [ "$status" -eq 1 ]
  run bash scripts/gsd/drive.sh status --run nothere
  [ "$status" -eq 1 ]; [[ "$output" == 'DRIVE:unknown run=nothere' ]]
}

@test "resume restores GSD_PLANNING_SYNC, TMPDIR and GSD_EXTRA_WRITABLE_WORKTREE from drive state" {
  printf '78\n' > "$STUB_ROOT/plan-1"
  mkdir -p "$BATS_TEST_TMPDIR/resume-tmp"
  run env GSD_PLANNING_SYNC=repo TMPDIR="$BATS_TEST_TMPDIR/resume-tmp" GSD_EXTRA_WRITABLE_WORKTREE=/resume/wt bash scripts/gsd/drive.sh start --verb plan-phase --phases 1-2 --on-blocked park --run envkeys
  [ "$status" -eq 0 ]
  run env -u GSD_PLANNING_SYNC -u GSD_EXTRA_WRITABLE_WORKTREE TMPDIR=/other bash scripts/gsd/drive.sh resume --run envkeys
  [ "$status" -eq 0 ]
  [ "$(jq -r '.runtime_env.GSD_PLANNING_SYNC' .planning/run-state/drive-envkeys.json)" = repo ]
  [ "$(tail -1 "$STUB_ROOT/runtime2")" = "repo $BATS_TEST_TMPDIR/resume-tmp /resume/wt" ]
}

@test "resume keeps a caller-provided GSD_PLANNING_SYNC when an older state file lacks the key" {
  printf '78\n' > "$STUB_ROOT/plan-1"
  run env -u GSD_PLANNING_SYNC bash scripts/gsd/drive.sh start --verb plan-phase --phases 1-2 --on-blocked park --run legacyenv
  [ "$status" -eq 0 ]
  jq 'del(.runtime_env.GSD_PLANNING_SYNC)' .planning/run-state/drive-legacyenv.json > "$BATS_TEST_TMPDIR/legacy.json"
  mv "$BATS_TEST_TMPDIR/legacy.json" .planning/run-state/drive-legacyenv.json
  run env GSD_PLANNING_SYNC=repo bash scripts/gsd/drive.sh resume --run legacyenv
  [ "$status" -eq 0 ]
  [ "$(tail -1 "$STUB_ROOT/runtime2" | cut -d' ' -f1)" = repo ]
}

@test "resume drops a saved TMPDIR that no longer exists and says so" {
  printf '78\n' > "$STUB_ROOT/plan-1"
  mkdir "$BATS_TEST_TMPDIR/gone-tmp"
  run env TMPDIR="$BATS_TEST_TMPDIR/gone-tmp" bash scripts/gsd/drive.sh start --verb plan-phase --phases 1-2 --on-blocked park --run gonetmp
  [ "$status" -eq 0 ]
  rmdir "$BATS_TEST_TMPDIR/gone-tmp"
  run env -u TMPDIR bash scripts/gsd/drive.sh resume --run gonetmp
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRIVE:tmpdir-missing run=gonetmp"* ]]
  [ "$(tail -1 "$STUB_ROOT/runtime2" | cut -d' ' -f2)" = unset ]
}

@test "resume unsets a key that was saved as null even when the caller passes a value" {
  printf '78\n' > "$STUB_ROOT/plan-1"
  run env -u GSD_PLANNING_SYNC bash scripts/gsd/drive.sh start --verb plan-phase --phases 1-2 --on-blocked park --run nullkey
  [ "$status" -eq 0 ]
  [ "$(jq -c '.runtime_env.GSD_PLANNING_SYNC' .planning/run-state/drive-nullkey.json)" = null ]
  run env GSD_PLANNING_SYNC=repo bash scripts/gsd/drive.sh resume --run nullkey
  [ "$status" -eq 0 ]
  [ "$(tail -1 "$STUB_ROOT/runtime2" | cut -d' ' -f1)" = unset ]
}

@test "project selection is restored from drive state when resumed without the caller environment" {
  printf '78\n' > "$STUB_ROOT/plan-1"
  run bash scripts/gsd/drive.sh start --verb plan-phase --phases 1-2 --project 443 --on-blocked park --run project
  [ "$status" -eq 0 ]
  run env -u GSD_PROJECT bash scripts/gsd/drive.sh resume --run project
  [ "$status" -eq 0 ]
  [ "$(cat "$STUB_ROOT/projects")" = $'443\n443' ]
}

@test "after-step and progress use the owned runner worktree when the driver runs from a consumer worktree" {
  mkdir -p "$REPO/.claude/worktrees"
  git worktree add -q --detach "$REPO/.claude/worktrees/cmo"
  git worktree add -q --detach "$REPO/.claude/worktrees/gated-p1"
  touch "$REPO/.claude/worktrees/gated-p1/produced-plan"
  cd "$REPO/.claude/worktrees/cmo"
  run bash "$REPO/scripts/gsd/drive.sh" start --verb plan-phase --phases 1 --run gated --after-step 'test -f produced-plan'
  [ "$status" -eq 0 ]
  [ "$(jq -r '.steps[0].state' .planning/run-state/drive-gated.json)" = done ]
  [ "$(jq -r '.steps[0].attempts' .planning/run-state/drive-gated.json)" = 0 ]
}

@test "resume restores explicit runtime settings without persisting unrelated environment values" {
  printf '78\n' > "$STUB_ROOT/plan-1"
  run env FFS_HOST=codex TIMEOUT=5400 GSD_NETWORK_MODE=enabled GSD_MODEL_REQUEST='{"kind":"tier","name":"judgment"}' UNRELATED_TOKEN=private-test-value bash scripts/gsd/drive.sh start --verb plan-phase --phases 1-2 --on-blocked park --run runtime
  [ "$status" -eq 0 ]
  run env -u FFS_HOST -u TIMEOUT -u GSD_NETWORK_MODE -u GSD_MODEL_REQUEST bash scripts/gsd/drive.sh resume --run runtime
  [ "$status" -eq 0 ]
  [ "$(wc -l < "$STUB_ROOT/runtime" | tr -d ' ')" = 2 ]
  [ "$(sort -u "$STUB_ROOT/runtime")" = 'codex 5400 enabled {"kind":"tier","name":"judgment"}' ]
  ! grep -q 'private-test-value\|UNRELATED_TOKEN' .planning/run-state/drive-runtime.json
}
