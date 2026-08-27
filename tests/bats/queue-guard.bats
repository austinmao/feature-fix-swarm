#!/usr/bin/env bats
# Wave-0 runner-boundary contract for REQ-204..208, 212 and 213.
# Every fixture is a real local Git topology.  Only the listed external-effect
# executables are made available in MOCK_BIN, and all reject unknown argv.
# Plan 02-01 completed the [GUARD] and [LOCK] placeholders; plan 02-02
# completed [CLASSIFY], [JOURNAL], and [EDGE-006] into GREEN-side assertions
# and reconciled $JOURNAL to the canonical skills/land-queue location.

setup() {
  # Hermetic supported-host identity for every fixture: managed sandboxes
  # deny `ps`/`sysctl`, so the suite supplies its own deterministic process
  # and boot identity (mirrors tests/bats/takeover-check.bats).  Production
  # inertness is proven there by the fail-closed unset-seam denied-PATH case.
  export TAKEOVER_TEST_IDENTITY="bats-boot-1"
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  GUARD="$ROOT/scripts/gsd/queue-guard.sh"
  JOURNAL="$ROOT/skills/land-queue/scripts/queue-journal.py"
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

  # REQ-204 boundary contract: walls trip AT the boundary, not one past it.
  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 10)) --item-started $((now - 5400)) --round 1 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:item-wall" ]

  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 10)) --item-started $((now - 5399)) --round 1 --now "$now"
  [ "$status" -eq 0 ]
  [ "$output" = "ALLOW" ]

  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 28801)) --item-started $((now - 5)) --round 1 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:queue-wall" ]

  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 28800)) --item-started $((now - 5)) --round 1 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:queue-wall" ]

  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 28799)) --item-started $((now - 5)) --round 1 --now "$now"
  [ "$status" -eq 0 ]
  [ "$output" = "ALLOW" ]

  # Operator STOP marker wins even when every cap is satisfied.
  touch "$STORE/STOP"
  run bash "$GUARD" allow --store "$STORE" --items 2 --queue-started $((now - 10)) --item-started $((now - 5)) --round 1 --now "$now"
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:operator-stop" ]

  # The guard decides before any collector or effect boundary is touched.
  [ ! -s "$CALL_LOG" ]
}

@test "[GUARD] repeated normalized failure blocks as no-progress via gates.py" {
  export GATES_STORE="$BATS_TEST_TMPDIR/gstore/evidence.json"
  mkdir -p "$BATS_TEST_TMPDIR/gstore"
  ERR="$BATS_TEST_TMPDIR/np-stderr"

  # First observation of a signature is progress (REQ-205 via the existing
  # gates.py note-failure authority — no second failure-history store).
  printf 'AssertionError at /tmp/wt-42/foo.py:12 expected 7\n' > "$ERR"
  run bash "$GUARD" note-failure --queue-id q1 --item spec/queue --gate review --stderr-file "$ERR"
  [ "$status" -eq 0 ]
  [ "$output" = "PROGRESS-OK" ]

  # Different digits and different paths, same shape: normalization strips
  # both, the signature repeats, and the item blocks (REQ-205).
  printf 'AssertionError at /tmp/wt-99/bar.py:77 expected 9\n' > "$ERR"
  run bash "$GUARD" note-failure --queue-id q1 --item spec/queue --gate review --stderr-file "$ERR"
  [ "$status" -eq 6 ]
  [ "$output" = "BLOCKED:no-progress" ]

  # A different gate is a different signature — never cross-contaminated.
  printf 'AssertionError at /tmp/wt-99/bar.py:77 expected 9\n' > "$ERR"
  run bash "$GUARD" note-failure --queue-id q1 --item spec/queue --gate ci --stderr-file "$ERR"
  [ "$status" -eq 0 ]
  [ "$output" = "PROGRESS-OK" ]

  # Genuine evidence-store I/O failure is the reserved rc-75 machine surface.
  mkdir -p "$BATS_TEST_TMPDIR/badstore/evidence.json"
  printf 'whatever\n' > "$ERR"
  GATES_STORE="$BATS_TEST_TMPDIR/badstore/evidence.json" \
    run bash "$GUARD" note-failure --queue-id q1 --item spec/queue --gate review --stderr-file "$ERR"
  [ "$status" -eq 75 ]
  grep -q "GATES-STORE-ERROR" <<<"$output"

  # Corrupt evidence JSON is the same typed surface straight from gates.py;
  # semantic refusal tokens and return codes stay untouched elsewhere.
  mkdir -p "$BATS_TEST_TMPDIR/cstore"
  printf '{broken' > "$BATS_TEST_TMPDIR/cstore/evidence.json"
  GATES_STORE="$BATS_TEST_TMPDIR/cstore/evidence.json" \
    run python3 "$ROOT/lib/gates.py" note-failure q1 --sig "review|x"
  [ "$status" -eq 75 ]
  grep -q "GATES-STORE-ERROR" <<<"$output"
}

@test "[CLASSIFY] enumerated systemic classes alone abort consecutively" {
  git -C "$WORK" diff --name-only main...spec/queue | grep -qx queue.txt
  ERR="$BATS_TEST_TMPDIR/stderr"
  BSTORE="$BATS_TEST_TMPDIR/bstore"; mkdir -p "$BSTORE"

  classify() { bash "$GUARD" classify-subprocess --boundary "$1" --rc "$2" --stderr-file "$ERR"; }

  # Precedence 1: CI-watch rc 124 is a LOCAL timeout, never systemic.
  : > "$ERR"
  run classify ci-watch 124
  [ "$status" -eq 0 ]
  [ "$output" = "local:ci-timeout" ]

  # Precedence 2: gates rc 75 plus the exact token is systemic store-error...
  printf 'GATES-STORE-ERROR: evidence store unreadable\n' > "$ERR"
  run classify gates 75
  [ "$status" -eq 0 ]
  [ "$output" = "store-error" ]
  # ...but the token without rc 75, or rc 75 without the token, stays local.
  run classify gates 1
  [ "$output" = "local" ]
  printf 'semantic refusal text\n' > "$ERR"
  run classify gates 75
  [ "$output" = "local" ]

  # Precedence 3: gh authentication signatures.
  printf 'gh: Not logged in to any GitHub hosts\n' > "$ERR"
  run classify gh 1
  [ "$output" = "gh-auth" ]
  printf 'HTTP 401: Bad credentials (https://api.github.com/graphql)\n' > "$ERR"
  run classify gh 1
  [ "$output" = "gh-auth" ]
  printf 'authentication required\n' > "$ERR"
  run classify gh 1
  [ "$output" = "gh-auth" ]

  # Precedence 4: gh/reviewer transport signatures.
  printf 'could not resolve host: api.github.com\n' > "$ERR"
  run classify gh 1
  [ "$output" = "network" ]
  printf 'connection refused\n' > "$ERR"
  run classify reviewer 1
  [ "$output" = "network" ]
  printf 'connection reset by peer\n' > "$ERR"
  run classify gh 1
  [ "$output" = "network" ]
  printf 'connection timed out\n' > "$ERR"
  run classify gh 1
  [ "$output" = "network" ]
  printf 'TLS handshake failure\n' > "$ERR"
  run classify gh 1
  [ "$output" = "network" ]

  # Precedence 5: reviewer hard-unavailability rc AFTER the signature checks.
  : > "$ERR"
  for rc in 124 126 127; do
    run classify reviewer "$rc"
    [ "$output" = "reviewer-unreachable" ]
  done
  # Reviewer findings at any other rc are an item-local defect.
  printf '2 blocking findings\n' > "$ERR"
  run classify reviewer 1
  [ "$output" = "local" ]

  # Collision resistance: signature text NOT at line start never classifies.
  printf 'test failed: expected connection refused in log output\n' > "$ERR"
  run classify gh 1
  [ "$output" = "local" ]
  printf 'assert: transcript mentions HTTP 401 somewhere\n' > "$ERR"
  run classify gh 1
  [ "$output" = "local" ]
  # Unknown/ambiguous outcomes are local; unknown boundaries are refused.
  printf 'entirely novel failure text\n' > "$ERR"
  run classify implement 1
  [ "$output" = "local" ]
  run classify vendor 1
  [ "$status" -eq 2 ]

  # Breaker accounting (6e4616bc, stricter reading): ANY two consecutive
  # enumerated systemic failures abort — class-agnostic.
  run bash "$GUARD" record --store "$BSTORE" --queue-id qa --class network
  [ "$status" -eq 0 ]
  [ "$output" = "RECORDED:systemic:network" ]
  run bash "$GUARD" record --store "$BSTORE" --queue-id qa --class gh-auth
  [ "$status" -eq 5 ]
  [ "$output" = "QUEUE-ABORTED:systemic:gh-auth" ]

  # An intervening local class resets the consecutive sequence...
  run bash "$GUARD" record --store "$BSTORE" --queue-id qb --class store-error
  [ "$status" -eq 0 ]
  run bash "$GUARD" record --store "$BSTORE" --queue-id qb --class local
  [ "$status" -eq 0 ]
  [ "$output" = "RECORDED:reset" ]
  run bash "$GUARD" record --store "$BSTORE" --queue-id qb --class reviewer-unreachable
  [ "$status" -eq 0 ]
  [ "$output" = "RECORDED:systemic:reviewer-unreachable" ]
  run bash "$GUARD" record --store "$BSTORE" --queue-id qb --class success
  [ "$status" -eq 0 ]
  # ...and local classes never abort, however many times they repeat.
  for _ in 1 2 3; do
    run bash "$GUARD" record --store "$BSTORE" --queue-id qb --class local
    [ "$status" -eq 0 ]
  done
  run bash "$GUARD" record --store "$BSTORE" --queue-id qb --class local:ci-timeout
  [ "$status" -eq 0 ]
  # Closed vocabulary: arbitrary class text is refused, never counted.
  run bash "$GUARD" record --store "$BSTORE" --queue-id qb --class totally-bogus
  [ "$status" -eq 2 ]
}

@test "[JOURNAL] hostile records preserve intent/result and idempotency key" {
  test -d "$LINKED/.git" || test -f "$LINKED/.git"
  STORE="$BATS_TEST_TMPDIR/jstore"; mkdir -p "$STORE"
  OID="$(git -C "$WORK" rev-parse refs/heads/spec/queue)"

  python3 "$JOURNAL" init --store "$STORE" --queue-id q1 --run-id r1

  # Intent carries the effect's idempotency key (PR number + head OID)
  # in the intent itself, before the effect runs (REQ-213 / 8c88ebfa).
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind intent --step merge --item spec/queue --pr 101 --head "$OID"
  [ "$status" -eq 0 ]
  run python3 "$JOURNAL" events --store "$STORE" --queue-id q1
  [ "$status" -eq 0 ]
  grep -q "pr-101@$OID" <<<"$output"

  # The dangling-intent reader surfaces the unresolved merge intent...
  DANG="$BATS_TEST_TMPDIR/dangling"
  python3 "$JOURNAL" read-dangling --store "$STORE" --queue-id q1 > "$DANG"
  fields=()
  while IFS= read -r -d '' v; do fields+=("$v"); done < "$DANG"
  [ "${#fields[@]}" -eq 4 ]
  [ "${fields[0]}" = "spec/queue" ]
  [ "${fields[1]}" = "merge" ]
  [ "${fields[2]}" = "101" ]
  [ "${fields[3]}" = "$OID" ]

  # ...and the observed result completes it append-only, never rewritten.
  python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind result --step merge --item spec/queue --status ok
  python3 "$JOURNAL" read-dangling --store "$STORE" --queue-id q1 > "$DANG"
  [ ! -s "$DANG" ]

  cp "$STORE/q1.json" "$BATS_TEST_TMPDIR/q1.before"

  # Closed validation: half-specified key, malformed PR, short head,
  # unknown step/action, and control bytes in scalars all refuse.
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind intent --step merge --item spec/queue --pr 102
  [ "$status" -eq 70 ]
  grep -q "QUEUE-ERROR:store" <<<"$output"
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind intent --step merge --item spec/queue --pr 10x --head "$OID"
  [ "$status" -eq 70 ]
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind intent --step merge --item spec/queue --pr 102 --head deadbeef
  [ "$status" -eq 70 ]
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind intent --step exfiltrate --item spec/queue
  [ "$status" -eq 70 ]
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind intent --step merge --item "$(printf 'evil\rname')"
  [ "$status" -eq 70 ]

  # Hostile persisted bytes: wrong schema version.
  printf '{"schema": 2, "queue_id": "q2", "events": []}' > "$STORE/q2.json"
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q2 --kind intent --step rebase
  [ "$status" -eq 70 ]
  grep -q "QUEUE-ERROR:store" <<<"$output"

  # Traversal-bearing id is rejected before path construction.
  run python3 "$JOURNAL" events --store "$STORE" --queue-id "../q1"
  [ "$status" -eq 70 ]

  # Symlinked journal file, symlinked store directory, non-regular file,
  # and oversized journal each fail closed with the named store error.
  ln -s "$STORE/q1.json" "$STORE/qlink.json"
  run python3 "$JOURNAL" events --store "$STORE" --queue-id qlink
  [ "$status" -eq 70 ]
  grep -q "QUEUE-ERROR:store" <<<"$output"
  ln -s "$STORE" "$BATS_TEST_TMPDIR/storelink"
  run python3 "$JOURNAL" events --store "$BATS_TEST_TMPDIR/storelink" --queue-id q1
  [ "$status" -eq 70 ]
  mkdir -p "$STORE/qdir.json"
  run python3 "$JOURNAL" events --store "$STORE" --queue-id qdir
  [ "$status" -eq 70 ]
  dd if=/dev/zero of="$STORE/qbig.json" bs=1 count=1 seek=5242880 2>/dev/null
  run python3 "$JOURNAL" events --store "$STORE" --queue-id qbig
  [ "$status" -eq 70 ]

  # Oversized scalar field is refused before any write.
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind result --step merge --item spec/queue --detail "$(printf 'x%.0s' $(seq 1 5000))"
  [ "$status" -eq 70 ]

  # Unwritable store: the append fails closed without consuming state.
  chmod 500 "$STORE"
  run python3 "$JOURNAL" append --store "$STORE" --queue-id q1 \
    --kind result --step ci --item spec/queue --status ok
  [ "$status" -eq 70 ]
  grep -q "QUEUE-ERROR:store" <<<"$output"
  chmod 700 "$STORE"

  # Every rejection above left the valid journal byte-identical.
  cmp -s "$STORE/q1.json" "$BATS_TEST_TMPDIR/q1.before"
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

  # Corrupt persisted state is a typed infrastructure error: QUEUE-ERROR:store,
  # never an item BLOCKED:* and never a systemic QUEUE-ABORTED alias.
  run python3 "$JOURNAL" events --store "$BATS_TEST_TMPDIR/store" --queue-id queue
  [ "$status" -eq 70 ]
  grep -q "QUEUE-ERROR:store" <<<"$output"
  ! grep -q "BLOCKED:" <<<"$output"
  ! grep -q "QUEUE-ABORTED" <<<"$output"

  # The shipped runner fails closed the same way BEFORE touching any
  # collector/vendor/effect boundary when its own journal state is hostile.
  export GATES_STORE="$BATS_TEST_TMPDIR/gstore/evidence.json"
  LQ="$BATS_TEST_TMPDIR/gstore/land-queue"; mkdir -p "$LQ"
  printf '{broken' > "$LQ/edge006.json"
  PATH="$MOCK_BIN:$PATH" CALL_LOG="$CALL_LOG" run bash "$QUEUE" \
    --repo "$WORK" --base main --run-id edge006 spec/queue
  [ "$status" -eq 70 ]
  grep -q "QUEUE-ERROR:store" <<<"$output"
  ! grep -q "QUEUE-ABORTED:systemic" <<<"$output"
  [ ! -s "$CALL_LOG" ]
}

@test "[GUARD] allow honors the recorded absolute --deadline over the reproduced wall" {
  # LOW-2 (round 4): the resume path loads the journal's IMMUTABLE recorded
  # deadline but the guard only ever REPRODUCED it from --queue-started.
  # The recorded value is the authority: pass it through and stop on it.
  STORE="$BATS_TEST_TMPDIR/qstore"; mkdir -p "$STORE"
  now=1000000

  # fresh queue clock but an EXPIRED recorded deadline — the RECORDED wins
  run bash "$GUARD" allow --store "$STORE" --items 2 \
    --queue-started $((now - 10)) --item-started $((now - 5)) \
    --round 1 --now "$now" --deadline $((now - 1))
  [ "$status" -eq 3 ]
  [ "$output" = "STOP:queue-wall" ]

  # an unexpired recorded deadline still allows
  run bash "$GUARD" allow --store "$STORE" --items 2 \
    --queue-started $((now - 10)) --item-started $((now - 5)) \
    --round 1 --now "$now" --deadline $((now + 100))
  [ "$status" -eq 0 ]
  [ "$output" = "ALLOW" ]
}
