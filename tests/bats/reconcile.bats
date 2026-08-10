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

@test "claim held by another reconciler is a typed rc 0 no-op" {
  now=$(date +%s); bash scripts/gsd/lifecycle.sh checkpoint held waiting wait time "{\"wake_at\":$((now-1))}" '["scripts/gsd/resume-stub.sh","held"]' '{"respawns":2}'
  jq ".wake_at=$((now-1))" .planning/run-state/lifecycle-held.json > record && mv record .planning/run-state/lifecycle-held.json
  # FFS_COORD_ANCHOR_PID pinned to this test's own durable pid (alive for the
  # whole test) makes coord.py see this claim as a live foreign holder — the
  # same idiom used by tests/bats/coord-claim.bats for a not-reclaimable claim.
  run env FFS_COORD_ANCHOR_PID="$$" python3 scripts/coord/coord.py claim reconcile-held
  [ "$status" -eq 0 ]
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:claim-held run=held'* ]]
  sleep 0.1; [ ! -e "$RECONCILE_MARK" ]
  [ "$(jq -r .budgets.respawns .planning/run-state/lifecycle-held.json)" = 2 ]
}

@test "budget at zero transitions record to failed without relaunch" {
  now=$(date +%s); bash scripts/gsd/lifecycle.sh checkpoint zero waiting wait time "{\"wake_at\":$((now-1))}" '["scripts/gsd/resume-stub.sh","zero"]' '{"respawns":0}'
  jq ".wake_at=$((now-1))" .planning/run-state/lifecycle-zero.json > record && mv record .planning/run-state/lifecycle-zero.json
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]
  [[ "$output" == *'RECONCILE:budget-exhausted run=zero'* ]]
  [ "$(jq -r .state .planning/run-state/lifecycle-zero.json)" = failed ]
  sleep 0.1; [ ! -e "$RECONCILE_MARK" ]
}

@test "idempotence: two sequential passes launch once and charge once" {
  now=$(date +%s); bash scripts/gsd/lifecycle.sh checkpoint idem waiting wait time "{\"wake_at\":$((now-1))}" '["scripts/gsd/resume-stub.sh","idem"]' '{"respawns":2}'
  jq ".wake_at=$((now-1))" .planning/run-state/lifecycle-idem.json > record && mv record .planning/run-state/lifecycle-idem.json
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:relaunched run=idem'* ]]
  sleep 0.2
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *still-waiting* ]]
  [ "$(wc -l < "$RECONCILE_MARK" | tr -d ' ')" = 1 ]
  [ "$(jq -r .budgets.respawns .planning/run-state/lifecycle-idem.json)" = 1 ]
}

@test "FFS_LIFECYCLE=off still recovers an existing record" {
  now=$(date +%s); bash scripts/gsd/lifecycle.sh checkpoint offed waiting wait time "{\"wake_at\":$((now-1))}" '["scripts/gsd/resume-stub.sh","offed"]' '{"respawns":2}'
  jq ".wake_at=$((now-1))" .planning/run-state/lifecycle-offed.json > record && mv record .planning/run-state/lifecycle-offed.json
  run env FFS_LIFECYCLE=off bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:relaunched run=offed'* ]]
  sleep 0.1; [ "$(cat "$RECONCILE_MARK")" = offed ]
  [ "$(jq -r .budgets.respawns .planning/run-state/lifecycle-offed.json)" = 1 ]
}

@test "time round trip: reset 3s out relaunches after expiry with verbatim argv" {
  now=$(date +%s); bash scripts/gsd/lifecycle.sh checkpoint roundtrip waiting wait time "{\"wake_at\":$((now+3))}" '["scripts/gsd/resume-stub.sh","verbatim","args"]' '{"respawns":2}'
  jq ".wake_at=$((now+3))" .planning/run-state/lifecycle-roundtrip.json > record && mv record .planning/run-state/lifecycle-roundtrip.json
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:still-waiting run=roundtrip'* ]]
  sleep 4
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:relaunched run=roundtrip'* ]]
  sleep 0.1
  expected="$(jq -r '.resume_argv[1:] | join(" ")' .planning/run-state/lifecycle-roundtrip.json)"
  [ "$(cat "$RECONCILE_MARK")" = "$expected" ]
}

@test "ci round trip: infra failure reruns then success relaunches" {
  # Local to this test only: bring in ci-watch.sh + its run-bounded helper and
  # stub gh on PATH, mirroring tests/bats/ci-watch.bats's setup idiom.
  cp "$ROOT/scripts/gsd/ci-watch.sh" "$ROOT/scripts/gsd/run-bounded.sh" "$REPO/scripts/gsd/"
  chmod +x "$REPO/scripts/gsd/"*.sh
  BIN="$BATS_TEST_TMPDIR/bin"; mkdir -p "$BIN"; export PATH="$BIN:$PATH"
  bash scripts/gsd/lifecycle.sh checkpoint ci waiting ci ci-completed '{"databaseId":42}' '["scripts/gsd/resume-stub.sh","ci"]' '{"respawns":2,"ci_reruns":2}'
  cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *--json* ]]; then echo '{"databaseId":42,"status":"completed","conclusion":"failure","attempt":1}'; elif [[ "$*" == *--log-failed* ]]; then echo runner-lost; elif [[ "$*" == *rerun* ]]; then :; fi
EOF
  chmod +x "$BIN/gh"
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:still-waiting run=ci'* ]]
  [ "$(jq -r .budgets.ci_reruns .planning/run-state/lifecycle-ci.json)" = 1 ]
  [ "$(jq -r .state .planning/run-state/lifecycle-ci.json)" = waiting ]

  cat > "$BIN/gh" <<'EOF'
#!/usr/bin/env bash
echo '{"databaseId":42,"status":"completed","conclusion":"success","attempt":2}'
EOF
  chmod +x "$BIN/gh"
  run bash scripts/gsd/reconcile.sh
  [ "$status" -eq 0 ]; [[ "$output" == *'RECONCILE:relaunched run=ci'* ]]
}

# "foreign-claimed run is untouched": reconcile.sh's only coord claim key
# per run is claim_id() -> "reconcile-<run>" (reconcile.sh:26-30) — there is
# no separate "run's own key" it ever claims, so a foreign hold on that one
# key is already exercised end-to-end by "claim held by another reconciler
# is a typed rc 0 no-op" above; no distinguishable second case exists.
