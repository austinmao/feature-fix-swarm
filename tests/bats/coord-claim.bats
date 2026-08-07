#!/usr/bin/env bats

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  COORD="$ROOT/scripts/coord/coord.py"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
  git -C "$REPO" init -q -b main
  git -C "$REPO" config user.email t@t
  git -C "$REPO" config user.name t
  git -C "$REPO" commit -q --allow-empty -m init
}

# ── Task 1: end-to-end "claim spec-009" — one path through every layer ─────

@test "claim spec-009 prints session then CLAIM-OK, exits 0, writes generation-1 holder" {
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  [[ "${lines[0]}" == session=* ]]
  [[ "$output" == *"CLAIM-OK generation=1"* ]]
  registry="$REPO/.feature-fix-swarm/coord/registry.json"
  [ -f "$registry" ]
  run python3 -c "import json; d=json.load(open('$registry')); e=d['claims']['claim:spec-009']; print(e['generation'])"
  [ "$status" -eq 0 ]
  [ "$output" = "1" ]
}

@test "status lists spec-009 holder, generation, and expiry after the claiming process is gone" {
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]

  run env -C "$REPO" python3 "$COORD" status
  [ "$status" -eq 0 ]
  [[ "$output" == *"claim:spec-009"* ]]
  [[ "$output" == *"generation=1"* ]]
  [[ "$output" == *"expires_at="* ]]
}

@test "identity round-trips: exporting the emitted session= value resolves the same identity" {
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  session_line="${lines[0]}"
  sid="${session_line#session=}"

  run env -C "$REPO" FFS_COORD_SESSION="$sid" python3 "$COORD" status
  [ "$status" -eq 0 ]
}

@test "coord.py outside any git repository exits 78" {
  outside="$BATS_TEST_TMPDIR/not-a-repo"
  mkdir -p "$outside"
  run env -C "$outside" python3 "$COORD" claim spec-009
  [ "$status" -eq 78 ]
}

@test "malformed spec-id is rejected with exit 2 before any write" {
  run env -C "$REPO" python3 "$COORD" claim "../etc"
  [ "$status" -eq 2 ]
  [ ! -f "$REPO/.feature-fix-swarm/coord/registry.json" ]
}

@test "symlinked store root is refused before any write" {
  mkdir -p "$REPO/.feature-fix-swarm"
  real="$BATS_TEST_TMPDIR/real-coord"
  mkdir -p "$real"
  ln -s "$real" "$REPO/.feature-fix-swarm/coord"
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 78 ]
  [ ! -e "$real/registry.json" ]
}

@test "a by-run pointer with non-uuid content exits 69 naming the pointer, not repaired" {
  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  pointer="$REPO/.feature-fix-swarm/coord/sessions/by-run/r1"
  [ -f "$pointer" ]
  printf 'not-a-uuid' > "$pointer"
  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" claim spec-009
  [ "$status" -eq 69 ]
  [[ "$output" == *"r1"* ]]
  content="$(cat "$pointer")"
  [ "$content" = "not-a-uuid" ]
}

# ── Task 2: claim atomicity and contention ──────────────────────────────────
# Both racers are backgrounded directly in the test's own shell (not inside a
# helper's command substitution) so `wait $pid` targets a direct child — a
# grandchild backgrounded inside $(...) cannot be `wait`-ed by pid in bash.

@test "20-rep different-identity race yields exactly one CLAIM-OK and one CLAIM-HELD, never two of either" {
  for i in $(seq 1 20); do
    rm -rf "$REPO/.feature-fix-swarm"
    a_log="$BATS_TEST_TMPDIR/diff-a-$i.log"
    b_log="$BATS_TEST_TMPDIR/diff-b-$i.log"
    ( set +e; cd "$REPO" && FFS_RUN_ID="race-a-$i" python3 "$COORD" claim spec-009 >"$a_log" 2>&1
      echo $? > "$a_log.rc" ) &
    pid_a=$!
    ( set +e; cd "$REPO" && FFS_RUN_ID="race-b-$i" python3 "$COORD" claim spec-009 >"$b_log" 2>&1
      echo $? > "$b_log.rc" ) &
    pid_b=$!
    wait "$pid_a" || true
    wait "$pid_b" || true
    rc_a="$(cat "$a_log.rc")"
    rc_b="$(cat "$b_log.rc")"
    ok=0; held=0
    [ "$rc_a" -eq 0 ] && ok=$((ok + 1))
    [ "$rc_a" -eq 3 ] && held=$((held + 1))
    [ "$rc_b" -eq 0 ] && ok=$((ok + 1))
    [ "$rc_b" -eq 3 ] && held=$((held + 1))
    if [ "$ok" -ne 1 ] || [ "$held" -ne 1 ]; then
      echo "iteration $i: rc_a=$rc_a rc_b=$rc_b" >&2
      cat "$a_log" "$b_log" >&2
      return 1
    fi
  done
}

@test "20-rep same-run-id race yields two CLAIM-OKs sharing one session, one claims entry, one generation bump" {
  for i in $(seq 1 20); do
    rm -rf "$REPO/.feature-fix-swarm"
    a_log="$BATS_TEST_TMPDIR/same-a-$i.log"
    b_log="$BATS_TEST_TMPDIR/same-b-$i.log"
    ( set +e; cd "$REPO" && FFS_RUN_ID="same-run-$i" python3 "$COORD" claim spec-009 >"$a_log" 2>&1
      echo $? > "$a_log.rc" ) &
    pid_a=$!
    ( set +e; cd "$REPO" && FFS_RUN_ID="same-run-$i" python3 "$COORD" claim spec-009 >"$b_log" 2>&1
      echo $? > "$b_log.rc" ) &
    pid_b=$!
    wait "$pid_a" || true
    wait "$pid_b" || true
    rc_a="$(cat "$a_log.rc")"
    rc_b="$(cat "$b_log.rc")"
    if [ "$rc_a" -ne 0 ] || [ "$rc_b" -ne 0 ]; then
      echo "iteration $i: rc_a=$rc_a rc_b=$rc_b" >&2
      cat "$a_log" "$b_log" >&2
      return 1
    fi
    sess_a="$(head -1 "$a_log")"
    sess_b="$(head -1 "$b_log")"
    [ "$sess_a" = "$sess_b" ]
    registry="$REPO/.feature-fix-swarm/coord/registry.json"
    run python3 -c "
import json
d = json.load(open('$registry'))
assert len(d['claims']) == 1, d['claims']
assert d['generations']['claim:spec-009']['gen'] == 1, d['generations']
"
    [ "$status" -eq 0 ]
  done
}

@test "different-identity race across a linked worktree still yields exactly one CLAIM-OK" {
  git -C "$REPO" worktree add -q "$BATS_TEST_TMPDIR/wt" -b wtbranch
  for i in 1 2 3; do
    rm -rf "$REPO/.feature-fix-swarm"
    a_log="$BATS_TEST_TMPDIR/wt-a-$i.log"
    b_log="$BATS_TEST_TMPDIR/wt-b-$i.log"
    ( set +e; cd "$REPO" && FFS_RUN_ID="wt-a-$i" python3 "$COORD" claim spec-009 >"$a_log" 2>&1
      echo $? > "$a_log.rc" ) &
    pid_a=$!
    ( set +e; cd "$BATS_TEST_TMPDIR/wt" && FFS_RUN_ID="wt-b-$i" python3 "$COORD" claim spec-009 >"$b_log" 2>&1
      echo $? > "$b_log.rc" ) &
    pid_b=$!
    wait "$pid_a" || true
    wait "$pid_b" || true
    rc_a="$(cat "$a_log.rc")"
    rc_b="$(cat "$b_log.rc")"
    ok=0; held=0
    [ "$rc_a" -eq 0 ] && ok=$((ok + 1))
    [ "$rc_a" -eq 3 ] && held=$((held + 1))
    [ "$rc_b" -eq 0 ] && ok=$((ok + 1))
    [ "$rc_b" -eq 3 ] && held=$((held + 1))
    [ "$ok" -eq 1 ]
    [ "$held" -eq 1 ]
  done
}

@test "env-carried identity: re-claiming via exported session= is idempotent, generation unchanged" {
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  sid="${lines[0]#session=}"

  run env -C "$REPO" FFS_COORD_SESSION="$sid" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  [[ "$output" == *"CLAIM-OK generation=1"* ]]

  run env -C "$REPO" python3 "$COORD" status
  [[ "$output" == *"generation=1"* ]]
}

@test "run-id-carried identity: second claim with same FFS_RUN_ID is idempotent, generation unchanged" {
  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  [[ "$output" == *"CLAIM-OK generation=1"* ]]
}

@test "a foreign session claiming an already-held spec exits 3 naming holder and expiry" {
  run env -C "$REPO" FFS_RUN_ID=holder python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"

  run env -C "$REPO" FFS_RUN_ID=other python3 "$COORD" claim spec-009
  [ "$status" -eq 3 ]
  [[ "$output" == *"CLAIM-HELD"* ]]
  [[ "$output" == *"$holder_sid"* ]]
  [[ "$output" == *"expires_at"* ]]
}

# ── Task 3: staleness, fencing, release, doctor/status ──────────────────────

@test "REQ-04: a claim survives while its anchor lives, reclaims once the anchor is killed, and the superseded holder is fenced" {
  # spawn a real background anchor process; claim under it; let the
  # claiming CLI itself exit (it always does — every coord.py invocation is
  # short-lived) — the claim must still be held.
  ( sleep 60 ) &
  anchor_pid=$!

  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$anchor_pid" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]

  # claiming CLI is long gone; anchor still alive -> a peer is refused
  run env -C "$REPO" FFS_RUN_ID=peer python3 "$COORD" claim spec-009
  [ "$status" -eq 3 ]

  # now kill the anchor -> the peer reclaims at generation 2
  kill "$anchor_pid" 2>/dev/null || true
  wait "$anchor_pid" 2>/dev/null || true
  run env -C "$REPO" FFS_RUN_ID=peer python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  [[ "$output" == *"generation=2"* ]]

  # the superseded original holder's claim-check at generation 1 is fenced
  run env -C "$REPO" python3 "$COORD" claim-check spec-009 --generation 1
  [ "$status" -eq 4 ]
  [[ "$output" == *"CLAIM-SUPERSEDED"* ]]

  # the new holder's claim-check at generation 2 succeeds
  run env -C "$REPO" FFS_RUN_ID=peer python3 "$COORD" claim-check spec-009 --generation 2
  [ "$status" -eq 0 ]
}

@test "REQ-12: doctor exits 69 COORD-UNAVAILABLE with a shimmed filelock lacking the version floor, no traceback" {
  shim_dir="$BATS_TEST_TMPDIR/shim-old"
  mkdir -p "$shim_dir"
  cat > "$shim_dir/filelock.py" <<'PYEOF'
__version__ = "3.29.0"
class FileLock: pass
class Timeout(Exception): pass
PYEOF
  run env -C "$REPO" PYTHONPATH="$shim_dir" python3 "$COORD" doctor
  [ "$status" -eq 69 ]
  [[ "$output" == *"COORD-UNAVAILABLE"* ]]
  [[ "$output" != *"Traceback"* ]]

  run env -C "$REPO" PYTHONPATH="$shim_dir" python3 "$COORD" claim spec-009
  [ "$status" -eq 69 ]
  [[ "$output" == *"COORD-UNAVAILABLE"* ]]
}

@test "REQ-12: doctor exits 0 with the real filelock and reports version/store/mode/live-claims" {
  run env -C "$REPO" python3 "$COORD" claim spec-009
  [ "$status" -eq 0 ]
  run env -C "$REPO" python3 "$COORD" doctor
  [ "$status" -eq 0 ]
  [[ "$output" == *"filelock_version="* ]]
  [[ "$output" == *"store_path="* ]]
  [[ "$output" == *"mode="* ]]
  [[ "$output" == *"live_claims=1"* ]]
}
