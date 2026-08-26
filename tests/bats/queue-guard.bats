#!/usr/bin/env bats
# Wave-0 runner-boundary contract for REQ-204..208, 212 and 213.
# Every fixture is a real local Git topology.  Only the listed external-effect
# executables are made available in MOCK_BIN, and all reject unknown argv.
# Plan 02-01 completed the [GUARD] and [LOCK] placeholders into GREEN-side
# assertions; [CLASSIFY], [JOURNAL], and [EDGE-006] stay typed RED for 02-02.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  GUARD="$ROOT/scripts/gsd/queue-guard.sh"
  JOURNAL="$ROOT/scripts/gsd/queue-journal.py"
  QUEUE="$ROOT/scripts/gsd/land-queue.sh"
  ORIGIN="$BATS_TEST_TMPDIR/origin.git"; WORK="$BATS_TEST_TMPDIR/work"; LINKED="$BATS_TEST_TMPDIR/linked"
  MOCK_BIN="$BATS_TEST_TMPDIR/boundaries"; CALL_LOG="$BATS_TEST_TMPDIR/calls"
  mkdir -p "$MOCK_BIN"; : > "$CALL_LOG"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=wave0 GIT_AUTHOR_EMAIL=wave0@example.invalid GIT_COMMITTER_NAME=wave0 GIT_COMMITTER_EMAIL=wave0@example.invalid
  unset GH_TOKEN GITHUB_TOKEN GIT_ASKPASS
  git init -q --bare "$ORIGIN"
  git init -q -b main "$WORK"; cd "$WORK"
  echo base > README.md; git add README.md; git commit -qm base
  git remote add origin "$ORIGIN"; git push -q origin main
  git checkout -qb spec/queue; echo changed > queue.txt; git add queue.txt; git commit -qm queue; git push -q origin spec/queue
  git checkout -q main; git worktree add -q "$LINKED" spec/queue
  for boundary in gh codex claude feature-implement assert-merged.sh run-finalizer.sh; do
    cat > "$MOCK_BIN/$boundary" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' "$0" "$@" >> "${CALL_LOG:?}"
printf 'UNSTUBBED-BOUNDARY:%s\n' "$*" >&2
exit 64
STUB
    chmod +x "$MOCK_BIN/$boundary"
  done
}

expect_red_target() {
  local tag="$1" target="$2" behavior="$3"
  if [ ! -f "$target" ]; then
    printf 'RED-EXPECTED: [%s] %s\n' "$tag" "$behavior" >&2
    return 1
  fi
  # The future GREEN suite executes shipped first-party code, never a shadow.
  PATH="$MOCK_BIN:$PATH" CALL_LOG="$CALL_LOG" bash "$target" --contract-probe
  printf 'RED-EXPECTED: [%s] %s\n' "$tag" "$behavior" >&2
  return 1
}

@test "[GUARD] caps reject before collector or effect boundaries" {
  git -C "$WORK" rev-parse --verify spec/queue >/dev/null
  [ ! -s "$CALL_LOG" ]
  if [ ! -f "$GUARD" ]; then
    printf 'RED-EXPECTED: [GUARD] round/item/queue/max-item cap enforcement absent\n' >&2
    return 1
  fi
  STORE="$BATS_TEST_TMPDIR/qstore"; mkdir -p "$STORE"
  now=1000000

  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 10)) --item-started $((now - 5)) --round 1 --now "$now"
  [ "$status" -eq 0 ]
  [ "$output" = "ALLOW" ]

  run bash "$GUARD" allow --store "$STORE" --items 11 --queue-started $((now - 10)) --item-started $((now - 5)) --round 1 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:max-items" ]

  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 10)) --item-started $((now - 5)) --round 3 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:round-cap" ]

  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 10)) --item-started $((now - 5401)) --round 1 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:item-wall" ]

  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 28801)) --item-started $((now - 5)) --round 1 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:queue-wall" ]

  # Operator STOP marker wins even when every cap is satisfied.
  touch "$STORE/STOP"
  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 10)) --item-started $((now - 5)) --round 1 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:operator-stop" ]

  # The guard decides before any collector or effect boundary is touched.
  [ ! -s "$CALL_LOG" ]
}

@test "[CLASSIFY] enumerated systemic classes alone abort consecutively" {
  git -C "$WORK" diff --name-only main...spec/queue | grep -qx queue.txt
  expect_red_target CLASSIFY "$GUARD" "normalized classifier matrix and collision resistance absent"
}

@test "[JOURNAL] hostile records preserve intent/result and idempotency key" {
  test -d "$LINKED/.git" || test -f "$LINKED/.git"
  expect_red_target JOURNAL "$JOURNAL" "validated append-only journal and recovery absent"
}

@test "[LOCK] link-published owner lock handles stale reclaim and signals" {
  test -f "$LINKED/.git"
  JOURNAL_PY="$ROOT/skills/land-queue/scripts/queue-journal.py"
  if [ ! -f "$QUEUE" ] || [ ! -f "$JOURNAL_PY" ]; then
    printf 'RED-EXPECTED: [LOCK] single-flight owner-only lock lifecycle absent\n' >&2
    return 1
  fi
  export GATES_STORE="$BATS_TEST_TMPDIR/gstore/evidence.json"
  LQ="$BATS_TEST_TMPDIR/gstore/land-queue"; mkdir -p "$LQ"

  # Acquire publishes a link-published owner lock bound to a live pid.
  run python3 "$JOURNAL_PY" lock-acquire --store "$LQ" --run-id r1 --pid $$
  [ "$status" -eq 0 ]
  [ -f "$LQ/queue.lock" ]

  # Single-flight: a live owner refuses a second acquire with the typed verdict.
  run python3 "$JOURNAL_PY" lock-acquire --store "$LQ" --run-id r2 --pid $$
  [ "$status" -eq 75 ]
  grep -q "QUEUE-REFUSED:queue-live" <<<"$output"

  # The shipped runner refuses a live queue before touching any boundary.
  PATH="$MOCK_BIN:$PATH" CALL_LOG="$CALL_LOG" run bash "$QUEUE" --repo "$WORK" --base main --run-id r2 spec/queue
  [ "$status" -eq 75 ]
  grep -q "QUEUE-REFUSED:queue-live" <<<"$output"
  [ ! -s "$CALL_LOG" ]

  # Owner-only release: a non-owner pid cannot release the lock.
  run python3 "$JOURNAL_PY" lock-release --store "$LQ" --pid 4194303
  [ "$status" -ne 0 ]
  [ -f "$LQ/queue.lock" ]
  run python3 "$JOURNAL_PY" lock-release --store "$LQ" --pid $$
  [ "$status" -eq 0 ]
  [ ! -f "$LQ/queue.lock" ]

  # Crash/signal story: a lock owned by a dead pid is judged stale and
  # reclaimed; the reclaimer then owns the queue.
  bash -c ':' &
  deadpid=$!
  wait "$deadpid"
  run python3 "$JOURNAL_PY" lock-acquire --store "$LQ" --run-id r3 --pid "$deadpid"
  [ "$status" -eq 0 ]
  run python3 "$JOURNAL_PY" lock-acquire --store "$LQ" --run-id r4 --pid $$
  [ "$status" -eq 0 ]
  [ -f "$LQ/queue.lock" ]
  run python3 "$JOURNAL_PY" lock-release --store "$LQ" --pid $$
  [ "$status" -eq 0 ]
}

@test "[EDGE-006] corrupt or unsafe queue store fails closed distinctly" {
  mkdir -p "$BATS_TEST_TMPDIR/store"; printf '{broken' > "$BATS_TEST_TMPDIR/store/queue.json"
  expect_red_target EDGE-006 "$JOURNAL" "QUEUE-ERROR:store validation absent"
}
