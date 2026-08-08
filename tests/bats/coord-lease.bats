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

# ── Task 1: end-to-end "exclusive lease blocks a foreign session" ──────────

@test "lease-acquire exclusive prints session then LEASE-OK, exits 0, writes one holder" {
  run env -C "$REPO" python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]
  [[ "${lines[0]}" == session=* ]]
  [[ "$output" == *"LEASE-OK generation=1"* ]]
  registry="$REPO/.feature-fix-swarm/coord/registry.json"
  [ -f "$registry" ]
  run python3 -c "
import json
d = json.load(open('$registry'))
e = d['leases']['path:docs/a.md']
assert e['mode'] == 'exclusive', e
assert len(e['holders']) == 1, e
"
  [ "$status" -eq 0 ]
}

@test "a second session requesting the same resource in either mode exits 3 naming the holder" {
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=holder python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]
  holder_sid="${lines[0]#session=}"

  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=other python3 "$COORD" lease-acquire --resource path:docs/a.md --mode shared
  [ "$status" -eq 3 ]
  [[ "$output" == *"LEASE-HELD"* ]]
  # adhoc 2026-08-08: refusal output is redacted to the 8-char prefix — the
  # full uuid is the FFS_COORD_SESSION impersonation token and this output
  # prints into the FOREIGN session's transcript.
  [[ "$output" == *"${holder_sid:0:8}"* ]]
  [[ "$output" != *"$holder_sid"* ]]
  # P2-W4: assert VALUE SHAPE, not bare field names -- a holder rendered as
  # `anchor_pid=None` satisfies a bare-name substring check completely.
  [[ "$output" =~ anchor_pid=[0-9]+ ]]
  [[ "$output" =~ worktree=/ ]]
  [[ "$output" =~ expires_at=[0-9] ]]

  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=other2 python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 3 ]
}

@test "a non-overlapping resource is granted while the first is refused" {
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=holder python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=other python3 "$COORD" lease-acquire --resource path:src/b.md --mode exclusive
  [ "$status" -eq 0 ]
}

@test "status lists the lease with mode/holder/generation/expiry; doctor reports live_leases and lease_holders" {
  run env -C "$REPO" python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]

  run env -C "$REPO" python3 "$COORD" status
  [ "$status" -eq 0 ]
  [[ "$output" == *"path:docs/a.md"* ]]
  [[ "$output" == *"mode=exclusive"* ]]
  [[ "$output" == *"generation=1"* ]]
  [[ "$output" == *"expires_at="* ]]

  run env -C "$REPO" python3 "$COORD" doctor
  [ "$status" -eq 0 ]
  [[ "$output" == *"live_leases=1"* ]]
  [[ "$output" == *"lease_holders=1"* ]]
}

@test "the symlink-escape form exits 78; the fourteen rejected resource forms exit 2 before any write" {
  mkdir -p "$BATS_TEST_TMPDIR/elsewhere"
  ln -s "$BATS_TEST_TMPDIR/elsewhere" "$REPO/escaped"
  run env -C "$REPO" python3 "$COORD" lease-acquire --resource path:escaped/a.md --mode exclusive
  [ "$status" -eq 78 ]

  for bad in "docs/a.md" "path:" "path:/**" "path:**" "path:docs/*.md" "path:docs/?.md" \
             "path:docs/[ab].md" "path:/etc/passwd" "path:../outside" \
             "path:docs/../../outside" "path:docs//a.md" "path:docs/./a.md" "path:docs/"; do
    run env -C "$REPO" python3 "$COORD" lease-acquire --resource "$bad" --mode exclusive
    [ "$status" -eq 2 ]
  done
  [ ! -f "$REPO/.feature-fix-swarm/coord/registry.json" ]
}

# ── Task 2: conflict matrix + overlap race ──────────────────────────────────

@test "shared+shared join: two different sessions both hold the same shared key" {
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=a python3 "$COORD" lease-acquire --resource path:docs/a.md --mode shared
  [ "$status" -eq 0 ]
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=b python3 "$COORD" lease-acquire --resource path:docs/a.md --mode shared
  [ "$status" -eq 0 ]
  registry="$REPO/.feature-fix-swarm/coord/registry.json"
  run python3 -c "
import json
d = json.load(open('$registry'))
assert len(d['leases']['path:docs/a.md']['holders']) == 2
"
  [ "$status" -eq 0 ]
}

@test "cross-key overlap: a prefix lease blocks an exact descendant path in either direction" {
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=a python3 "$COORD" lease-acquire --resource "path:skills/**" --mode exclusive
  [ "$status" -eq 0 ]
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=b python3 "$COORD" lease-acquire --resource path:skills/feature-implement/SKILL.md --mode shared
  [ "$status" -eq 3 ]
}

@test "20-rep exclusive race yields exactly one LEASE-OK and one LEASE-HELD, never two of either" {
  for i in $(seq 1 20); do
    rm -rf "$REPO/.feature-fix-swarm"
    a_log="$BATS_TEST_TMPDIR/lease-a-$i.log"
    b_log="$BATS_TEST_TMPDIR/lease-b-$i.log"
    ( set +e; cd "$REPO" && FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="lease-race-a-$i" python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive >"$a_log" 2>&1
      echo $? > "$a_log.rc" ) &
    pid_a=$!
    ( set +e; cd "$REPO" && FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="lease-race-b-$i" python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive >"$b_log" 2>&1
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

@test "shared first-creation race: two concurrent shared first-acquires both exit 0 with two holders" {
  rm -rf "$REPO/.feature-fix-swarm"
  a_log="$BATS_TEST_TMPDIR/shared-a.log"
  b_log="$BATS_TEST_TMPDIR/shared-b.log"
  ( set +e; cd "$REPO" && FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="shared-race-a" python3 "$COORD" lease-acquire --resource path:docs/shared.md --mode shared >"$a_log" 2>&1
    echo $? > "$a_log.rc" ) &
  pid_a=$!
  ( set +e; cd "$REPO" && FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="shared-race-b" python3 "$COORD" lease-acquire --resource path:docs/shared.md --mode shared >"$b_log" 2>&1
    echo $? > "$b_log.rc" ) &
  pid_b=$!
  wait "$pid_a" || true
  wait "$pid_b" || true
  [ "$(cat "$a_log.rc")" -eq 0 ]
  [ "$(cat "$b_log.rc")" -eq 0 ]
  registry="$REPO/.feature-fix-swarm/coord/registry.json"
  run python3 -c "
import json
d = json.load(open('$registry'))
assert len(d['leases']['path:docs/shared.md']['holders']) == 2
"
  [ "$status" -eq 0 ]
}

@test "cross-worktree exclusive race behaves identically" {
  git -C "$REPO" worktree add -q "$BATS_TEST_TMPDIR/wt" -b wtbranch
  for i in 1 2 3; do
    rm -rf "$REPO/.feature-fix-swarm"
    a_log="$BATS_TEST_TMPDIR/lease-wt-a-$i.log"
    b_log="$BATS_TEST_TMPDIR/lease-wt-b-$i.log"
    ( set +e; cd "$REPO" && FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="lease-wt-a-$i" python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive >"$a_log" 2>&1
      echo $? > "$a_log.rc" ) &
    pid_a=$!
    ( set +e; cd "$BATS_TEST_TMPDIR/wt" && FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID="lease-wt-b-$i" python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive >"$b_log" 2>&1
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

# ── Task 3: release/renew ────────────────────────────────────────────────

@test "acquire -> release -> re-acquire by a different session exits 0 at a strictly higher generation" {
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=a python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]
  [[ "$output" == *"generation=1"* ]]

  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=a python3 "$COORD" lease-release --resource path:docs/a.md --generation 1
  [ "$status" -eq 0 ]
  [[ "$output" == *"RELEASE-OK"* ]]

  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=b python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]
  [[ "$output" == *"generation=2"* ]]
}

@test "status shows no lease line after the last holder releases; doctor reports live_leases=0" {
  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]
  gen="${lines[1]#LEASE-OK generation=}"

  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" lease-release --resource path:docs/a.md --generation "$gen"
  [ "$status" -eq 0 ]

  run env -C "$REPO" python3 "$COORD" status
  [ "$status" -eq 0 ]
  [[ "$output" != *"path:docs/a.md"* ]]

  run env -C "$REPO" python3 "$COORD" doctor
  [ "$status" -eq 0 ]
  [[ "$output" == *"live_leases=0"* ]]
}

@test "lease-renew fences on the caller's own generation and exits 4 on mismatch" {
  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]

  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" lease-renew --resource path:docs/a.md --generation 99
  [ "$status" -eq 4 ]
  [[ "$output" == *"LEASE-SUPERSEDED"* ]]

  run env -C "$REPO" FFS_RUN_ID=r1 python3 "$COORD" lease-renew --resource path:docs/a.md --generation 1
  [ "$status" -eq 0 ]
  [[ "$output" == *"LEASE-OK"* ]]
}

@test "release requires holder uuid + generation; a foreign uuid or stale generation exits 3" {
  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=holder python3 "$COORD" lease-acquire --resource path:docs/a.md --mode exclusive
  [ "$status" -eq 0 ]

  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=other python3 "$COORD" lease-release --resource path:docs/a.md --generation 1
  [ "$status" -eq 3 ]
  [[ "$output" == *"RELEASE-REFUSED"* ]]

  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=holder python3 "$COORD" lease-release --resource path:docs/a.md --generation 99
  [ "$status" -eq 3 ]

  run env -C "$REPO" FFS_COORD_ANCHOR_PID="$$" FFS_RUN_ID=holder python3 "$COORD" lease-release --resource path:docs/a.md --generation 1
  [ "$status" -eq 0 ]
}
