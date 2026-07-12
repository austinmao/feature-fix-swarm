#!/usr/bin/env bats
# run-bounded.sh — portable wall-clock bound lib (timeout -> gtimeout ->
# python3 -> refuse rc-124). The invariant under test: an external CLI call is
# NEVER executed unbounded, on any host shape.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LIB="$REPO_ROOT/scripts/gsd/run-bounded.sh"
  ADVERSARY_HOST="$REPO_ROOT/scripts/gsd/adversary-host.sh"
  TMP="$(mktemp -d)"
}

teardown() { rm -rf "$TMP"; }

# Build a PATH dir holding ONLY the named system binaries (symlinks), so the
# resolution ladder can be exercised per-rung.
make_shim_path() {
  local dir="$1"; shift
  mkdir -p "$dir"
  local b
  for b in "$@"; do
    ln -s "$(command -v "$b")" "$dir/$b"
  done
}

@test "RB-001: passes through exit code of a fast command" {
  run bash -c ". '$LIB'; run_bounded 5 sh -c 'exit 3'"
  [ "$status" -eq 3 ]
}

@test "RB-002: bounds a hanging command at the wall (rc 124)" {
  # whatever rung the host resolves to (timeout/gtimeout/python3) must kill at ~1s
  run bash -c ". '$LIB'; run_bounded 1 sleep 30"
  [ "$status" -eq 124 ]
}

@test "RB-003: python3 rung bounds when timeout/gtimeout are absent" {
  make_shim_path "$TMP/shim" python3 sleep
  run env PATH="$TMP/shim" /bin/bash -c ". '$LIB'; run_bounded 1 sleep 30"
  [ "$status" -eq 124 ]
}

@test "RB-004: python3 rung passes through exit code" {
  make_shim_path "$TMP/shim" python3 sh
  run env PATH="$TMP/shim" /bin/bash -c ". '$LIB'; run_bounded 5 sh -c 'exit 7'"
  [ "$status" -eq 7 ]
}

@test "RB-005: no mechanism at all -> REFUSES rc 124 without running the command" {
  mkdir -p "$TMP/empty"
  # absolute command path: it COULD run — the refusal is what stops it
  run env PATH="$TMP/empty" /bin/bash -c ". '$LIB'; run_bounded 5 /usr/bin/touch '$TMP/ran'"
  [ "$status" -eq 124 ]
  [ ! -f "$TMP/ran" ]
  [[ "$output" == *"refusing unbounded"* ]]
}

@test "RB-006: adversary_invoke stays bounded on a coreutils-less host" {
  # regression for the old "run unwrapped rather than hard-fail" branch:
  # no timeout/gtimeout on PATH, hanging fake codex -> rc 124 at ~1s, not a block
  make_shim_path "$TMP/shim" python3 bash dirname sleep
  cat > "$TMP/codex-hang" <<'SH'
#!/bin/sh
sleep 30
SH
  chmod +x "$TMP/codex-hang"
  run env PATH="$TMP/shim" ADVERSARY_BIN_CODEX="$TMP/codex-hang" /bin/bash -c \
    ". '$ADVERSARY_HOST'; adversary_invoke codex 1 some-model high 'hi'"
  [ "$status" -eq 124 ]
}

@test "RB-007: review-gate ship gate fail-softs (never hangs) on a hung codex" {
  # hung codex shadowed onto PATH; GSD_REVIEW_TIMEOUT bounds at 1s; the gate
  # must return APPROVED fail-soft instead of blocking the ship forever
  mkdir -p "$TMP/shim" "$TMP/cwd"
  cat > "$TMP/shim/codex" <<'SH'
#!/bin/sh
sleep 30
SH
  chmod +x "$TMP/shim/codex"
  # HOME override: keeps a real ~/.claude/lib/feature-fix-swarm/gates.py from
  # engaging the grant wall (REVISE) before the codex call under test
  run env PATH="$TMP/shim:$PATH" HOME="$TMP" GSD_RUN_ID=spec-000 GSD_REVIEW_TIMEOUT=1 \
    /bin/bash -c "cd '$TMP/cwd' && echo 'diff --git a b' | /bin/bash '$REPO_ROOT/scripts/gsd/review-gate-command.sh'"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [[ "$output" == *'fail-soft'* ]]
}
