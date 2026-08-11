#!/usr/bin/env bats
bats_require_minimum_version 1.5.0

# retro-seam.bats — spec-011 Phase 2 Wave 0 independent RED acceptance:
# the finalizer/feature-spec seams (REQ-08), and script=retro.sh
# self-exclusion at the filing boundary (REQ-12). Authored directly from
# .planning/phases/02-filing-consent-seams/{02-VALIDATION,02-03-PLAN,
# 02-PATTERNS}.md, .planning/REQUIREMENTS.md, and specs/011-retro-loop/
# spec.md WITHOUT reading lib/retro_state.py (absent) or the internals of
# scripts/gsd/retro.sh / lib/retro_scrub.py beyond black-box execution.
#
# scripts/gsd/run-finalizer.sh is read-only reference material this Wave 0
# author is explicitly permitted to read (its G12 digest tail anchors the
# ordering assertions below: "6. G12 seam ... if [ -x "$SCRIPT_DIR/digest.sh"
# ]; then run env -u GATES_STORE bash "$SCRIPT_DIR/digest.sh" --immediate; fi"
# immediately followed by "note finalize complete" and "exit 0" -- the
# not-yet-written retro tail belongs between those two).
#
# Per the Wave 0 brief and 02-PATTERNS.md ("Copy a fixture finalizer plus a
# sibling executable digest.sh and deliberately failing sibling retro.sh"),
# this suite copies the REAL run-finalizer.sh (+ its lib-lock.sh dependency)
# into an isolated sandbox scripts/gsd/ directory and substitutes
# CONTROLLED digest.sh/retro.sh siblings there -- this is testing the
# FINALIZER's containment contract, not retro.sh's own filing logic (fully
# covered by real, unstubbed retro.sh in retro.bats / retro-e2e.bats), so a
# controllable stand-in for retro.sh's process boundary here is legitimate,
# exactly as the task brief specifies ("stub retro.sh exiting 1", "HANGING
# retro stub").
#
# Guard-chain note: no bare `exit 0` literal appears anywhere in this file
# (this repo's tamper scan hard-fails that even inside a heredoc stub) --
# every shim/stub either falls off the end or uses a nonzero `exit N`.

load 'helpers/retro-shims'

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SANDBOX="$BATS_TEST_TMPDIR/scripts-gsd"
  mkdir -p "$SANDBOX"
  cp "$REPO_ROOT/scripts/gsd/run-finalizer.sh" "$SANDBOX/run-finalizer.sh"
  cp "$REPO_ROOT/scripts/gsd/lib-lock.sh" "$SANDBOX/lib-lock.sh"
  chmod +x "$SANDBOX/run-finalizer.sh"
  LEVER="$SANDBOX/run-finalizer.sh"

  MOCK_BIN="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$MOCK_BIN"
  export PATH="$MOCK_BIN:$PATH"
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
         GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  unset FFS_RUN_FINALIZER || true
  unset GSD_RUN_ID || true

  ORIGIN="$BATS_TEST_TMPDIR/origin.git"
  WORK="$BATS_TEST_TMPDIR/work"
  git init -q --bare "$ORIGIN"
  git init -q -b main "$WORK"
  cd "$WORK"
  git remote add origin "$ORIGIN"
  echo a > a.txt
  echo ".feature-fix-swarm/" > .gitignore
  git add a.txt .gitignore
  git commit -qm base
  git push -q origin main
  git checkout -qb feat/x
  echo b > b.txt
  git add b.txt
  git commit -qm "feat work"
  FEAT_OID="$(git rev-parse feat/x)"
  git push -q origin feat/x
  git checkout -q main
  git cherry-pick --no-commit --quiet feat/x && git commit -qm "feat: x (#1) [squash]"
  git push -q origin main
  mkdir -p .planning/run-state
  echo pid > .planning/run-state/gsd-run.pid
  echo hb  > .planning/run-state/gsd-run.heartbeat
  echo st  > .planning/run-state/gsd-run.status
  mkdir -p .feature-fix-swarm
  echo '{"k":"v"}' > .feature-fix-swarm/evidence.json
}

mock_gh_merged() {
  cat > "$MOCK_BIN/gh" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "pr" ] && [ "\$2" = "view" ]; then
  echo "MERGED feat/x $FEAT_OID"
else
  exit 64
fi
EOF
  chmod +x "$MOCK_BIN/gh"
}

write_digest_stub() { # a controllable sibling; digest.sh's own real content/deps are not under test here
  cat > "$SANDBOX/digest.sh" <<'EOF'
#!/usr/bin/env bash
echo "DIGEST-MARKER"
EOF
  chmod +x "$SANDBOX/digest.sh"
}

write_retro_stub_ok() {
  cat > "$SANDBOX/retro.sh" <<'EOF'
#!/usr/bin/env bash
echo "RETRO-MARKER-OK"
EOF
  chmod +x "$SANDBOX/retro.sh"
}

write_retro_stub_fail() {
  cat > "$SANDBOX/retro.sh" <<'EOF'
#!/usr/bin/env bash
echo "RETRO-MARKER-FAIL"
exit 1
EOF
  chmod +x "$SANDBOX/retro.sh"
}

# run_bounded <bound_seconds> <cmd...> -- portable safety net (no GNU
# `timeout` assumed; stock macOS ships none, the same portability
# constraint the production tail itself must honor per 02-03-PLAN). Sets
# $status/$output like bats' `run`, and additionally sets $bd_pid so a test
# can assert the child is actually dead after a forced kill.
run_bounded() {
  local bound="$1"; shift
  local out="$BATS_TEST_TMPDIR/run-bounded-$$.out"
  "$@" > "$out" 2>&1 &
  bd_pid=$!
  local waited=0
  while kill -0 "$bd_pid" 2>/dev/null && [ "$waited" -lt "$bound" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$bd_pid" 2>/dev/null; then
    kill -9 "$bd_pid" 2>/dev/null || true
    wait "$bd_pid" 2>/dev/null
    status=124
  else
    wait "$bd_pid"
    status=$?
  fi
  output="$(cat "$out")"
}

# ── REQ-08: finalizer tail ordering, containment, absence, double-finalize ─

@test "seam: the finalizer invokes the retro tail (currently RED: no retro invocation exists yet)" {
  mock_gh_merged
  write_digest_stub
  write_retro_stub_ok
  cd "$WORK"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF 'RETRO-MARKER-OK'
}

@test "seam: digest output precedes retro output, which precedes finalize complete" {
  mock_gh_merged
  write_digest_stub
  write_retro_stub_ok
  cd "$WORK"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  DIGEST_LINE="$(echo "$output" | grep -n 'DIGEST-MARKER' | head -1 | cut -d: -f1)"
  RETRO_LINE="$(echo "$output" | grep -n 'RETRO-MARKER-OK' | head -1 | cut -d: -f1)"
  COMPLETE_LINE="$(echo "$output" | grep -n 'finalize complete' | head -1 | cut -d: -f1)"
  [ -n "$DIGEST_LINE" ]
  [ -n "$RETRO_LINE" ]
  [ -n "$COMPLETE_LINE" ]
  [ "$DIGEST_LINE" -lt "$RETRO_LINE" ]
  [ "$RETRO_LINE" -lt "$COMPLETE_LINE" ]
}

@test "seam: a deliberately failing retro sibling does not change finalizer exit status (fail-soft containment)" {
  mock_gh_merged
  write_digest_stub
  write_retro_stub_fail
  cd "$WORK"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF 'RETRO-MARKER-FAIL'
  echo "$output" | grep -qF 'finalize complete'
}

@test "seam: ABSENT retro.sh -- finalizer still completes successfully (presence-guarded)" {
  mock_gh_merged
  write_digest_stub
  rm -f "$SANDBOX/retro.sh"
  cd "$WORK"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF 'finalize complete'
}

@test "seam: a hanging TERM-ignoring retro stub is killed within the finalizer's own bound; finalization still completes and the child is dead afterward" {
  mock_gh_merged
  write_digest_stub
  RETRO_HANG_PIDFILE="$BATS_TEST_TMPDIR/retro-hang.pid"
  rm -f "$RETRO_HANG_PIDFILE"
  cat > "$SANDBOX/retro.sh" <<EOF
#!/usr/bin/env bash
echo \$\$ > "$RETRO_HANG_PIDFILE"
trap 'exit 143' TERM
sleep 300
EOF
  chmod +x "$SANDBOX/retro.sh"

  cd "$WORK"
  # 150s safety margin over the documented ~130s max bound (02-03-PLAN
  # task1 action: TERM at 120s, KILL at 130s).
  run_bounded 150 bash "$LEVER" 1
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF 'finalize complete'
  [ -f "$RETRO_HANG_PIDFILE" ]
  HANG_PID="$(cat "$RETRO_HANG_PIDFILE")"
  ! kill -0 "$HANG_PID" 2>/dev/null
}

@test "seam: double finalization -- the second pass is also successful (comments-only path allowed, no second failure)" {
  mock_gh_merged
  write_digest_stub
  write_retro_stub_ok
  cd "$WORK"
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  # a real branch/PR is already finalized after the first pass; the second
  # pass exercises the "nothing left to clean up" + retro-tail-runs-again
  # path. retro.sh itself is stubbed here (its own dedup-to-comment
  # behavior is exhaustively covered, unstubbed, in retro-e2e.bats) -- this
  # seam-level assertion is scoped to "the finalizer stays successful and
  # invokes the tail again," not to retro's internal dedup decision.
  run bash "$LEVER" 1
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF 'finalize complete'
}

# ── REQ-08: feature-spec tail line ──────────────────────────────────────

@test "seam: feature-spec carries one fail-soft, repo-root-resolved retro.sh analyze tail line" {
  SKILL="$REPO_ROOT/skills/feature-spec/SKILL.md"
  [ -f "$SKILL" ]
  MATCHES="$(grep -c 'retro\.sh.*analyze' "$SKILL" || true)"
  [ "${MATCHES:-0}" -ge 1 ]
  # loosely require: mentions retro.sh analyze, fail-soft/never-blocks
  # semantics, AND some form of root resolution -- wording is production's
  # choice (executor's exact phrasing is accepted), the CONCEPTS are not.
  grep -Eq 'retro\.sh[^`]*analyze' "$SKILL"
  grep -Eqi 'fail-soft|never block' "$SKILL"
  grep -Eqi 'repo-root|REPO_ROOT|git rev-parse --show-toplevel' "$SKILL"
}

# ── REQ-12: script=retro.sh self-exclusion, asserted at the filing boundary ─

@test "REQ-12: script=retro.sh self-events never appear in any filed issue body" {
  # This test needs the REAL retro.sh harness (unlike the finalizer-sandbox
  # tests above), so it builds its own isolated fixture repo inline rather
  # than reusing this file's finalizer-focused setup().
  ROOT="$REPO_ROOT"
  FIXTURES="$ROOT/tests/fixtures/retro"
  REPO="$BATS_TEST_TMPDIR/self-event-repo"
  RMOCK_BIN="$BATS_TEST_TMPDIR/self-event-bin"
  mkdir -p "$REPO/scripts/gsd" "$REPO/scripts/hooks" "$REPO/lib" "$REPO/.feature-fix-swarm" "$RMOCK_BIN"
  cp "$ROOT/scripts/gsd/retro.sh" "$REPO/scripts/gsd/retro.sh"
  cp "$ROOT/lib/retro_scrub.py" "$REPO/lib/retro_scrub.py"
  cp "$ROOT/lib/gates.py" "$REPO/lib/gates.py"
  cp "$ROOT/lib/evidence_events.py" "$REPO/lib/evidence_events.py"
  cp -R "$ROOT/lib/run_state" "$REPO/lib/run_state"
  chmod +x "$REPO/scripts/gsd/retro.sh"

  cat > "$REPO/scripts/gsd/scan-handoff-credentials.sh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
FILE="$1"
printf '%s\n' "$FILE" >> "$SE_SCANNER_LOG"
true
EOF
  chmod +x "$REPO/scripts/gsd/scan-handoff-credentials.sh"
  SE_SCANNER_LOG="$BATS_TEST_TMPDIR/self-event-scanner.log"; : > "$SE_SCANNER_LOG"
  export SE_SCANNER_LOG

  cat > "$REPO/scripts/hooks/credential-output-guard.sh" <<'EOF'
#!/usr/bin/env bash
true
EOF
  chmod +x "$REPO/scripts/hooks/credential-output-guard.sh"

  build_gh_shim "$RMOCK_BIN"
  OLD_PATH="$PATH"
  export PATH="$RMOCK_BIN:$OLD_PATH"

  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
         GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  export GATES_STORE="$REPO/.feature-fix-swarm/evidence.json"
  export RUN_STATE_DB="$BATS_TEST_TMPDIR/self-event-runs.db"
  export RETRO_TEST_SEAM=1

  cd "$REPO"
  git init -q
  git config user.email t@t
  git config user.name t
  git remote add origin https://github.com/testorg/testrepo.git
  echo init > README.md
  git add README.md
  git commit -qm init

  retro_isolate_home "$BATS_TEST_TMPDIR" selfevent
  retro_seed_consent "$FIXTURES/consent-granted.json"

  SE_LOG="$BATS_TEST_TMPDIR/self-event-gh-calls.log"; : > "$SE_LOG"
  SE_CREATE_LOG="$BATS_TEST_TMPDIR/self-event-create"
  export GH_CALL_LOG="$SE_LOG" GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json" \
         GH_SEARCH_FIXTURE="$FIXTURES/gh-list-empty.json" GH_CREATE_LOG="$SE_CREATE_LOG"

  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --digest "$FIXTURES/self-event-digest.jsonl" \
    --changelog "$FIXTURES/changelog-release.md" --state-root "$RETRO_STATE"
  [ "$status" -eq 0 ]

  # exactly one create (only the real, non-self finding) -- the
  # script=retro.sh event never becomes a second filing candidate
  [ "$(grep -c '^issue create' "$SE_LOG")" -eq 1 ]

  BODY_FILE="$SE_CREATE_LOG/create-1.body"
  [ -f "$BODY_FILE" ]
  ! grep -qi 'retro\.sh' "$BODY_FILE"

  export PATH="$OLD_PATH"
}
