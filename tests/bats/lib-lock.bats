#!/usr/bin/env bats
# lib-lock.sh — real-process coverage for the reusable pid/lease primitive.

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LOCK_LIB="$ROOT/scripts/gsd/lib-lock.sh"
  LOCK_DIR="$BATS_TEST_TMPDIR/locks"
  mkdir -p "$LOCK_DIR"
}

acquire() {
  bash -c '
    source "$1"
    ffs_lock_acquire "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9"
  ' _ "$LOCK_LIB" "$@"
}

@test "only one real subprocess owns a pid lock and only its release removes it" {
  lock="$LOCK_DIR/owner.pid"
  bash -c 'source "$1"; ffs_lock_acquire "$2" "" "$3/reclaim" local 120 30 lock ""; sleep 2; ffs_lock_release "$2" local' _ "$LOCK_LIB" "$lock" "$LOCK_DIR" &
  holder=$!
  for _ in $(seq 1 100); do [ -f "$lock" ] && break; sleep 0.02; done
  [ -f "$lock" ]

  run acquire "$lock" "" "$LOCK_DIR/reclaim" local 120 30 lock ""
  [ "$status" -eq 75 ]
  wait "$holder"
  [ ! -e "$lock" ]
}

@test "live same-host and fresh foreign lease are refused" {
  lock="$LOCK_DIR/live.pid"
  printf '%s\nmachine=local\nclaimed_epoch=%s\n' "$$" "$(date +%s)" > "$lock"
  run acquire "$lock" "" "$LOCK_DIR/reclaim" local 120 30 lock ""
  [ "$status" -eq 75 ]

  printf '999999\nmachine=foreign\nclaimed_epoch=%s\n' "$(date +%s)" > "$lock"
  run acquire "$lock" "" "$LOCK_DIR/reclaim" local 120 30 lock ""
  [ "$status" -eq 75 ]
}

@test "stale foreign ownership is reclaimed and lock paths reject symlinks" {
  lock="$LOCK_DIR/stale.pid"
  printf '999999\nmachine=foreign\nclaimed_epoch=1\n' > "$lock"
  source "$LOCK_LIB"
  ffs_lock_acquire "$lock" "" "$LOCK_DIR/reclaim" local 1 1 lock
  ffs_lock_release "$lock" local

  target="$LOCK_DIR/target"
  : > "$target"
  ln -s "$target" "$lock"
  run acquire "$lock" "" "$LOCK_DIR/reclaim" local 1 1 lock ""
  [ "$status" -eq 78 ]
}

@test "release preserves a changed owner record" {
  lock="$LOCK_DIR/changed.pid"
  source "$LOCK_LIB"
  ffs_lock_acquire "$lock" "" "$LOCK_DIR/reclaim" local 120 30 lock ""
  printf '999999\nmachine=other\nclaimed_epoch=1\n' > "$lock"
  ffs_lock_release "$lock" local
  [ -f "$lock" ]
}
