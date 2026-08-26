#!/usr/bin/env bats
# Wave-0 runner-boundary contract for REQ-204..208, 212 and 213.
# Every fixture is a real local Git topology.  Only the listed external-effect
# executables are made available in MOCK_BIN, and all reject unknown argv.

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
  expect_red_target GUARD "$GUARD" "round/item/queue/max-item cap enforcement absent"
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
  expect_red_target LOCK "$QUEUE" "single-flight owner-only lock lifecycle absent"
}

@test "[EDGE-006] corrupt or unsafe queue store fails closed distinctly" {
  mkdir -p "$BATS_TEST_TMPDIR/store"; printf '{broken' > "$BATS_TEST_TMPDIR/store/queue.json"
  expect_red_target EDGE-006 "$JOURNAL" "QUEUE-ERROR:store validation absent"
}
