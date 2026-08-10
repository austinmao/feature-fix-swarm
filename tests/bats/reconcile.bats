#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"; REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/.planning/run-state" "$REPO/scripts/gsd" "$REPO/scripts/coord"
  cp "$ROOT/scripts/gsd/lifecycle.sh" "$ROOT/scripts/gsd/reconcile.sh" "$REPO/scripts/gsd/"
  cp "$ROOT/scripts/coord/coord.py" "$REPO/scripts/coord/"
  cat > "$REPO/scripts/gsd/resume-stub.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${RECONCILE_MARK:?}"
EOF
  chmod +x "$REPO/scripts/gsd/"*.sh
  cd "$REPO"; git init -q -b main; git -c user.email=t -c user.name=t commit -q --allow-empty -m init
  export RECONCILE_MARK="$BATS_TEST_TMPDIR/mark"
}

@test "tracer: satisfied time condition relaunches once" {
  now=$(date +%s); bash scripts/gsd/lifecycle.sh checkpoint tracer waiting wait time "{\"wake_at\":$((now-1))}" '["scripts/gsd/resume-stub.sh","verbatim"]' '{"respawns":2}'
  jq ".wake_at=$((now-1))" .planning/run-state/lifecycle-tracer.json > record && mv record .planning/run-state/lifecycle-tracer.json
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *RECONCILE:relaunched* ]]
  sleep 0.1; [ "$(cat "$RECONCILE_MARK")" = verbatim ]; [ "$(jq -r .budgets.respawns .planning/run-state/lifecycle-tracer.json)" = 1 ]
}

@test "unsatisfied record stays byte identical" {
  now=$(date +%s); bash scripts/gsd/lifecycle.sh checkpoint later waiting wait time "{\"wake_at\":$((now+3600))}" '["scripts/gsd/resume-stub.sh"]' '{"respawns":1}'
  jq ".wake_at=$((now+3600))" .planning/run-state/lifecycle-later.json > record && mv record .planning/run-state/lifecycle-later.json
  before=$(cat .planning/run-state/lifecycle-later.json); run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [ "$(cat .planning/run-state/lifecycle-later.json)" = "$before" ]; [[ "$output" == *still-waiting* ]]
}

@test "kill switch is a typed no-op" {
  run env FFS_RECONCILE=off bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [ "$output" = RECONCILE:disabled ]
}

@test "invalid and terminal records are skipped without a launch" {
  printf '{truncated' > .planning/run-state/lifecycle-bad.json
  now=$(date +%s)
  bash scripts/gsd/lifecycle.sh checkpoint done done complete manual '{}' '["scripts/gsd/resume-stub.sh"]' '{"respawns":1}'
  before=$(cat .planning/run-state/lifecycle-done.json)
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]
  [[ "$output" == *'RECONCILE:invalid-record run=bad'* ]]
  [ "$(cat .planning/run-state/lifecycle-done.json)" = "$before" ]
  [ ! -e "$RECONCILE_MARK" ]
}

@test "a dead stale running launcher is recovered once" {
  now=$(date +%s)
  bash scripts/gsd/lifecycle.sh checkpoint stale running start time "{\"wake_at\":$((now-1))}" '["scripts/gsd/resume-stub.sh","stale"]' '{"respawns":2}'
  jq '.child_pid=99999999 | .launched_at=1' .planning/run-state/lifecycle-stale.json > record && mv record .planning/run-state/lifecycle-stale.json
  run env FFS_RECONCILE_STALE_SECS=1 bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:relaunched run=stale'* ]]
  sleep 0.1
  [ "$(cat "$RECONCILE_MARK")" = stale ]
  [ "$(jq -r .budgets.respawns .planning/run-state/lifecycle-stale.json)" = 2 ]
}
