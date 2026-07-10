#!/usr/bin/env bats
# liveness-check.sh — composite AND-of-death autonomous-run liveness detector.
# Signals: P (pidfile holds a live pid), M (state-dir mtime within window),
# G (ship:gsd grant in flight via gates.py check-grant). ALIVE (exit 0) iff
# P||M||G. DEAD (exit 1) ONLY when all three are false (the FFF row) — a
# single transient failed probe must never kill an autonomous run (AC-011,
# PATH-007, US8). Reference: EDGE-005 accepts a window-bounded false-alive
# when a pid is reused by an unrelated process.
#
# G is stubbed by PATH-shimming `python3` ahead of the real binary (canary-gate
# .bats stub convention) so gates.py's *content* never matters — only the exit
# code the stub returns for a `check-grant` invocation. This decouples the
# test from whichever of the 3 candidate gates.py paths actually resolves.

SCRIPT_REL="scripts/gsd/liveness-check.sh"
DEAD_PID=2147483647 # exceeds any real OS pid_max — kill -0 always ESRCH

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/$SCRIPT_REL"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/lib"
  cd "$REPO" || return 1
  git init -q
  git config user.email t@t
  git config user.name t
  echo init > README.md
  git add README.md
  git commit -q -m init
  # Guarantees the 3rd GATES_PY candidate resolves in any environment
  # (CI boxes without a real $HOME/.claude/.../gates.py). Content is
  # irrelevant — python3 itself is stubbed below.
  echo "# stub" > lib/gates.py

  PIDFILE="$BATS_TEST_TMPDIR/pidfile"
  STATEDIR="$BATS_TEST_TMPDIR/state"
  mkdir -p "$STATEDIR"
  BINSTUB="$BATS_TEST_TMPDIR/binstub"
  mkdir -p "$BINSTUB"
}

alive_pid_into() { # $1 = pidfile path — bats' own subshell pid, alive for the test's duration
  echo "$$" > "$1"
}

dead_pid_into() { # $1 = pidfile path
  echo "$DEAD_PID" > "$1"
}

garbage_pid_into() { # $1 = pidfile path
  echo "banana" > "$1"
}

fresh_state() { # $1 = state-dir
  mkdir -p "$1"
  rm -f "$1"/*
  touch "$1/f"
}

stale_state() { # $1 = state-dir
  mkdir -p "$1"
  rm -f "$1"/*
  touch -t 202001010000 "$1/f"
}

stub_python3() { # $1 = exit code the stub returns for a check-grant call
  cat > "$BINSTUB/python3" <<EOF
#!/usr/bin/env bash
if [[ "\$*" == *"check-grant"* ]]; then
  exit $1
fi
exit 1
EOF
  chmod +x "$BINSTUB/python3"
}

run_check() {
  PATH="$BINSTUB:$PATH" run bash "$SCRIPT" "$PIDFILE" "$STATEDIR" --run-id test-run
}

# ---- 8-row truth table (P, M, G) -----------------------------------------

@test "TTT: alive pid + fresh mtime + grant -> ALIVE" {
  alive_pid_into "$PIDFILE"; fresh_state "$STATEDIR"; stub_python3 0
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"P=alive"* ]]
  [[ "$output" == *"M=fresh"* ]]
  [[ "$output" == *"G=granted"* ]]
  [[ "$output" == *"ALIVE"* ]]
}

@test "TTF: alive pid + fresh mtime + no grant -> ALIVE (PATH-007)" {
  alive_pid_into "$PIDFILE"; fresh_state "$STATEDIR"; stub_python3 1
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"P=alive"* ]]
  [[ "$output" == *"M=fresh"* ]]
  [[ "$output" == *"G=not-granted"* ]]
  [[ "$output" == *"ALIVE"* ]]
}

@test "TFT: alive pid + stale mtime + grant -> ALIVE" {
  alive_pid_into "$PIDFILE"; stale_state "$STATEDIR"; stub_python3 0
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"P=alive"* ]]
  [[ "$output" == *"M=stale"* ]]
  [[ "$output" == *"G=granted"* ]]
  [[ "$output" == *"ALIVE"* ]]
}

@test "TFF: alive pid + stale mtime + no grant -> ALIVE (pid alone)" {
  alive_pid_into "$PIDFILE"; stale_state "$STATEDIR"; stub_python3 1
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"P=alive"* ]]
  [[ "$output" == *"M=stale"* ]]
  [[ "$output" == *"G=not-granted"* ]]
  [[ "$output" == *"ALIVE"* ]]
}

@test "FTT: dead pid + fresh mtime + grant -> ALIVE" {
  dead_pid_into "$PIDFILE"; fresh_state "$STATEDIR"; stub_python3 0
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"P=dead"* ]]
  [[ "$output" == *"M=fresh"* ]]
  [[ "$output" == *"G=granted"* ]]
  [[ "$output" == *"ALIVE"* ]]
}

@test "FTF: dead pid + fresh mtime + no grant -> ALIVE (mtime alone keeps it alive)" {
  dead_pid_into "$PIDFILE"; fresh_state "$STATEDIR"; stub_python3 1
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"P=dead"* ]]
  [[ "$output" == *"M=fresh"* ]]
  [[ "$output" == *"G=not-granted"* ]]
  [[ "$output" == *"ALIVE"* ]]
}

@test "FFT: dead pid + stale mtime + grant in flight -> ALIVE (grant alone keeps it alive)" {
  dead_pid_into "$PIDFILE"; stale_state "$STATEDIR"; stub_python3 0
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"P=dead"* ]]
  [[ "$output" == *"M=stale"* ]]
  [[ "$output" == *"G=granted"* ]]
  [[ "$output" == *"ALIVE"* ]]
}

@test "FFF: dead pid + stale mtime + no ship grant -> DEAD (the only DEAD row)" {
  dead_pid_into "$PIDFILE"; stale_state "$STATEDIR"; stub_python3 1
  run_check
  [ "$status" -eq 1 ]
  [[ "$output" == *"P=dead"* ]]
  [[ "$output" == *"M=stale"* ]]
  [[ "$output" == *"G=not-granted"* ]]
  [[ "$output" == *"DEAD"* ]]
}

# ---- garbage / missing pidfile: pid signal dies quietly, no crash --------

@test "garbage pidfile (non-numeric) counts as dead pid signal, no crash, still evaluates M/G" {
  garbage_pid_into "$PIDFILE"; fresh_state "$STATEDIR"; stub_python3 1
  run_check
  [ "$status" -eq 0 ]
  [[ "$output" == *"P=dead"* ]]
  [[ "$output" == *"M=fresh"* ]]
}

@test "missing pidfile counts as dead pid signal, no crash, FFF still DEAD" {
  rm -f "$PIDFILE"
  stale_state "$STATEDIR"; stub_python3 1
  run_check
  [ "$status" -eq 1 ]
  [[ "$output" == *"P=dead"* ]]
  [[ "$output" == *"M=stale"* ]]
  [[ "$output" == *"G=not-granted"* ]]
  [[ "$output" == *"DEAD"* ]]
}
