#!/usr/bin/env bats
bats_require_minimum_version 1.5.0

# retro-e2e.bats — spec-011 Phase 2 Wave 0 independent RED acceptance:
# dedup/search/similarity matching (REQ-06), caps/P3/pacing/write-outcome
# policy (REQ-07), and race/idempotency behavior over a scriptable `gh`
# PATH shim. Authored directly from
# .planning/phases/02-filing-consent-seams/{02-VALIDATION,02-01-PLAN,
# 02-02-PLAN}.md, .planning/REQUIREMENTS.md, and specs/011-retro-loop/
# {spec.md,edge-coverage.md} WITHOUT reading lib/retro_state.py (absent) or
# the internals of scripts/gsd/retro.sh / lib/retro_scrub.py beyond
# black-box execution. See tests/bats/helpers/retro-shims.bash for the
# shared harness and its documented ambiguity resolutions (isolated-HOME
# strategy, consent.json version scheme, gh --search detection, and the
# dynamic title-similarity construction this file's boundary tests use).
#
# Fingerprints baked into the static tests/fixtures/retro/gh-*.json files
# below were derived by running the REAL (Phase 1) `retro.sh analyze`
# black-box over the paired digest fixture with changelog-release.md
# (ffs_minor "1.4") -- not guessed, not read from lib/retro_scrub.py.
#
# Guard-chain note: no bare `exit 0` literal appears anywhere in this file
# (this repo's tamper scan hard-fails that even inside a heredoc stub) --
# every shim/stub either falls off the end or uses a nonzero `exit N`.

load 'helpers/retro-shims'

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  REPO="$BATS_TEST_TMPDIR/repo"
  MOCK_BIN="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$REPO/scripts/gsd" "$REPO/scripts/hooks" "$REPO/lib" "$REPO/.feature-fix-swarm" "$MOCK_BIN"

  cp "$ROOT/scripts/gsd/retro.sh" "$REPO/scripts/gsd/retro.sh"
  # fail-soft: absence must stay observable as RED, not error out the harness
  [ -f "$ROOT/lib/retro_state.py" ] && cp "$ROOT/lib/retro_state.py" "$REPO/lib/retro_state.py"
  cp "$ROOT/lib/retro_scrub.py" "$REPO/lib/retro_scrub.py"
  cp "$ROOT/lib/gates.py" "$REPO/lib/gates.py"
  cp "$ROOT/lib/evidence_events.py" "$REPO/lib/evidence_events.py"
  cp -R "$ROOT/lib/run_state" "$REPO/lib/run_state"
  chmod +x "$REPO/scripts/gsd/retro.sh"

  FIXTURES="$ROOT/tests/fixtures/retro"

  cat > "$REPO/scripts/gsd/scan-handoff-credentials.sh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
FILE="$1"
printf '%s\n' "$FILE" >> "$SCANNER_LOG"
if [ -f "${SCANNER_FAIL_FLAG:-/nonexistent-scanner-fail-flag}" ]; then
  exit 1
fi
true
EOF
  chmod +x "$REPO/scripts/gsd/scan-handoff-credentials.sh"
  SCANNER_LOG="$BATS_TEST_TMPDIR/scanner-calls.log"
  : > "$SCANNER_LOG"
  export SCANNER_LOG

  cat > "$REPO/scripts/hooks/credential-output-guard.sh" <<'EOF'
#!/usr/bin/env bash
true
EOF
  chmod +x "$REPO/scripts/hooks/credential-output-guard.sh"

  build_gh_shim "$MOCK_BIN"
  export PATH="$MOCK_BIN:$PATH"

  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
         GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  export GATES_STORE="$REPO/.feature-fix-swarm/evidence.json"
  export RUN_STATE_DB="$BATS_TEST_TMPDIR/runs.db"
  export RETRO_TEST_SEAM=1

  cd "$REPO" || return 1
  git init -q
  git config user.email t@t
  git config user.name t
  git remote add origin https://github.com/testorg/testrepo.git
  echo init > README.md
  git add README.md
  git commit -qm init
}

file_mode() { stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"; }

# analyze_h [extra retro.sh analyze args] -- isolated-HOME + matching
# --state-root wrapper (see retro-shims.bash header for why both).
analyze_h() {
  run --separate-stderr bash "$REPO/scripts/gsd/retro.sh" analyze \
    --changelog "$FIXTURES/changelog-release.md" --state-root "$RETRO_STATE" "$@"
}

fresh_env() { # fresh_env <label> -- isolated HOME + granted consent + clean gh call log
  retro_isolate_home "$BATS_TEST_TMPDIR" "$1"
  retro_seed_consent "$FIXTURES/consent-granted.json"
  GH_CALL_LOG="$BATS_TEST_TMPDIR/gh-calls-$1.log"; : > "$GH_CALL_LOG"
  GH_CREATE_LOG="$BATS_TEST_TMPDIR/gh-create-$1"
  GH_COMMENT_LOG="$BATS_TEST_TMPDIR/gh-comment-$1"
  GH_NEXT_ISSUE_FILE="$BATS_TEST_TMPDIR/gh-next-issue-$1"
  GH_COMMENT_COUNT_FILE="$BATS_TEST_TMPDIR/gh-comment-count-$1"
  export GH_CALL_LOG GH_CREATE_LOG GH_COMMENT_LOG GH_NEXT_ISSUE_FILE GH_COMMENT_COUNT_FILE
  unset GH_AUTH_FAIL GH_WRITE_FAIL_CODE GH_LIST_FIXTURE GH_SEARCH_FIXTURE GH_TIMING_LOG || true
}

count_calls() { # count_calls <prefix> -- grep -c prints "0" on no-match but
  # still exits 1, so capture output rather than relying on `||` (which
  # would append a SECOND "0", corrupting a later `-eq` comparison).
  local n
  n="$(grep -c "^$1" "$GH_CALL_LOG" 2>/dev/null)"
  printf '%s' "${n:-0}"
}

# ── REQ-06: exact / search-fallback / closed / lowest-number dedup ─────────

@test "dedup: exact fingerprint match in bounded list -> one comment on the LOWEST issue number, zero creates" {
  fresh_env exact-lowest
  GH_LIST_FIXTURE="$FIXTURES/gh-list-exact-multiple.json"; export GH_LIST_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]
  grep '^issue comment' "$GH_CALL_LOG" | grep -qE '(^| )501($| )'
  ! grep '^issue comment' "$GH_CALL_LOG" | grep -qE '(^| )777($| )'
}

@test "dedup: match only via search fallback (list lacks it, search has it) -> comment, zero creates, search actually called" {
  fresh_env search-fallback
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_SEARCH_FIXTURE="$FIXTURES/gh-search-fallback-match.json"
  export GH_LIST_FIXTURE GH_SEARCH_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]
  grep '^issue comment' "$GH_CALL_LOG" | grep -qE '(^| )888($| )'
  grep -q -- '--search' "$GH_CALL_LOG"
}

@test "dedup EDGE-007: closed-issue exact match -> comment on the closed issue, never reopened" {
  fresh_env closed-exact
  GH_LIST_FIXTURE="$FIXTURES/gh-list-exact-closed.json"; export GH_LIST_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]
  grep '^issue comment' "$GH_CALL_LOG" | grep -qE '(^| )601($| )'
  ! grep -qi 'reopen' "$GH_CALL_LOG"
  ! grep -qi -- '--state open' "$GH_CALL_LOG"
}

# ── REQ-06: title similarity boundary (inclusive 0.8) ───────────────────────
#
# The compared title is generated by not-yet-written production code, so it
# is captured from a REAL create call first, then used to construct exact
# ratio-0.8 / ratio-0.75 variants at run time (see
# tests/bats/helpers/retro_similar_title.py) instead of guessing a static
# fixture title.

capture_real_title() { # capture_real_title <label> -> writes $CAPTURED_TITLE
  fresh_env "cap-$1"
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"; export GH_LIST_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]
  CAPTURED_TITLE_FILE="$GH_CREATE_LOG/create-1.title"
  [ -f "$CAPTURED_TITLE_FILE" ]
  [ -s "$CAPTURED_TITLE_FILE" ]
}

@test "dedup: title similarity ratio exactly RETRO_TITLE_SIM (0.8, inclusive) -> comment" {
  capture_real_title sim08

  SIM_TITLE_FILE="$BATS_TEST_TMPDIR/sim08-title.txt"
  python3 "$ROOT/tests/bats/helpers/retro_similar_title.py" \
    "$CAPTURED_TITLE_FILE" 4 5 "$SIM_TITLE_FILE"
  SIM_TITLE="$(cat "$SIM_TITLE_FILE")"

  LIST_FIXTURE="$BATS_TEST_TMPDIR/gh-list-sim08.json"
  python3 - "$SIM_TITLE" "$LIST_FIXTURE" <<'PY'
import json, sys
title, out = sys.argv[1], sys.argv[2]
json.dump([{"number": 42, "title": title, "body": "no metadata comment here", "state": "OPEN"}], open(out, "w"))
PY

  fresh_env sim08-hit
  GH_LIST_FIXTURE="$LIST_FIXTURE"; export GH_LIST_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]
  grep '^issue comment' "$GH_CALL_LOG" | grep -qE '(^| )42($| )'
}

@test "dedup: title similarity just below threshold (0.75) -> proceeds to create after re-query" {
  capture_real_title simbelow

  SIM_TITLE_FILE="$BATS_TEST_TMPDIR/sim075-title.txt"
  python3 "$ROOT/tests/bats/helpers/retro_similar_title.py" \
    "$CAPTURED_TITLE_FILE" 3 4 "$SIM_TITLE_FILE"
  SIM_TITLE="$(cat "$SIM_TITLE_FILE")"

  LIST_FIXTURE="$BATS_TEST_TMPDIR/gh-list-sim075.json"
  python3 - "$SIM_TITLE" "$LIST_FIXTURE" <<'PY'
import json, sys
title, out = sys.argv[1], sys.argv[2]
json.dump([{"number": 43, "title": title, "body": "no metadata comment here", "state": "OPEN"}], open(out, "w"))
PY

  fresh_env sim075-miss
  GH_LIST_FIXTURE="$LIST_FIXTURE"; GH_SEARCH_FIXTURE="$LIST_FIXTURE"
  export GH_LIST_FIXTURE GH_SEARCH_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]
  [ "$(count_calls 'issue comment')" -eq 0 ]
}

# ── REQ-07: create cap ───────────────────────────────────────────────────

@test "cap: RETRO_MAX_NEW_ISSUES=3 permits exactly 3 creates on 4 distinct new findings; 4th accrues in the ledger" {
  fresh_env cap3
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"; export GH_LIST_FIXTURE
  RETRO_MAX_NEW_ISSUES=3 analyze_h --digest "$FIXTURES/cap-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 3 ]
  LEDGER="$RETRO_STATE/retro-ledger.jsonl"
  [ -f "$LEDGER" ]
  # the 4th finding did not create, but is not silently dropped either
  python3 -c "
import json
rows = [json.loads(l) for l in open('$LEDGER') if l.strip()]
fps = {r.get('fingerprint') for r in rows}
assert 'd37613c4267ccc77' in fps, rows  # 4th cap-digest finding's fingerprint
creates = [r for r in rows if r.get('action') == 'create']
assert len(creates) == 3, rows
"
}

@test "cap: RETRO_MAX_NEW_ISSUES=0 -> comments only path allowed, zero creates (EDGE-009, cap floors at comments-only)" {
  fresh_env cap0
  GH_LIST_FIXTURE="$FIXTURES/gh-list-exact-single.json"; export GH_LIST_FIXTURE
  RETRO_MAX_NEW_ISSUES=0 analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]
}

# ── REQ-07: P3 occurrence floor ─────────────────────────────────────────

@test "P3 floor: occurrences 1 and 2 accrue with no create; the 3rd occurrence creates (RETRO_P3_OCCURRENCE_FLOOR=3)" {
  fresh_env p3floor
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"; export GH_LIST_FIXTURE

  RETRO_P3_OCCURRENCE_FLOOR=3 analyze_h --digest "$FIXTURES/p3-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]

  RETRO_P3_OCCURRENCE_FLOOR=3 analyze_h --digest "$FIXTURES/p3-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]

  RETRO_P3_OCCURRENCE_FLOOR=3 analyze_h --digest "$FIXTURES/p3-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]
}

# ── REQ-07: persisted >=2s pacing ────────────────────────────────────────

@test "pacing: two consecutive writes are at least 2s apart per the persisted timestamp" {
  fresh_env pacing
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_TIMING_LOG="$BATS_TEST_TMPDIR/gh-timing-pacing.log"; : > "$GH_TIMING_LOG"
  export GH_LIST_FIXTURE GH_TIMING_LOG
  # two distinct new findings in one run -> two writes (creates) governed by
  # the SAME persisted pacing timestamp without releasing the lock.
  analyze_h --digest "$FIXTURES/cap-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(wc -l < "$GH_TIMING_LOG")" -ge 2 ]
  [ "$(gh_pacing_ok "$GH_TIMING_LOG" 2)" = "ok" ]
}

# ── REQ-07: known write-failure outcomes are fail-soft, no retry ────────

@test "gh write 403 -> typed value-free ledger row, rc 0, exactly one write attempt (no retry)" {
  fresh_env write403
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_WRITE_FAIL_CODE=403
  export GH_LIST_FIXTURE GH_WRITE_FAIL_CODE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]
  LEDGER="$RETRO_STATE/retro-ledger.jsonl"
  [ -f "$LEDGER" ]
  [ "$(wc -l < "$LEDGER")" -eq 1 ]
  ! grep -q 'WRITE-FAIL-MARKER' "$LEDGER"
  python3 -c "import json; json.loads(open('$RETRO_STATE/retro-ledger.jsonl').read().strip())"
}

@test "gh write 422 -> typed value-free ledger row, rc 0, exactly one write attempt (no retry)" {
  fresh_env write422
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_WRITE_FAIL_CODE=422
  export GH_LIST_FIXTURE GH_WRITE_FAIL_CODE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]
  LEDGER="$RETRO_STATE/retro-ledger.jsonl"
  [ -f "$LEDGER" ]
  [ "$(wc -l < "$LEDGER")" -eq 1 ]
  ! grep -q 'WRITE-FAIL-MARKER' "$LEDGER"
}

# ── REQ-06 idempotency / PATH-002 shape: rerun over already-filed payload ──

@test "idempotency: rerun over an already-filed payload -> comments only, zero new creates" {
  fresh_env idempotent
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"; export GH_LIST_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]

  # simulate gh now reflecting the created issue on rerun
  RERUN_LIST="$BATS_TEST_TMPDIR/gh-list-idempotent-rerun.json"
  cp "$GH_CREATE_LOG/create-1.title" "$BATS_TEST_TMPDIR/created-title.txt"
  python3 - "$BATS_TEST_TMPDIR/created-title.txt" "$RERUN_LIST" <<'PY'
import json, sys
title, out = sys.argv[1], sys.argv[2]
title = open(title).read()
body = "Automatically filed by ffs-retro.\n\n<!-- ffs-retro fingerprint:6f8560bdd33c6f56 priority:P1 occurrences:1 -->"
json.dump([{"number": 1, "title": title, "body": body, "state": "OPEN"}], open(out, "w"))
PY
  : > "$GH_CALL_LOG"
  GH_LIST_FIXTURE="$RERUN_LIST"; export GH_LIST_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]
}

# ── REQ-06/07: local ledger create record consulted BEFORE gh queries ──────

@test "dedup ordering: a rerun consults the local ledger create record before any gh list/search query" {
  fresh_env ledgerfirst
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"; export GH_LIST_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]

  # rerun: gh list/search fixtures deliberately still say "nothing exists"
  # (unset -> shim defaults to []) -- read-after-write lag simulation. If
  # the local ledger create record were NOT consulted first, this would
  # incorrectly create a duplicate.
  : > "$GH_CALL_LOG"
  unset GH_LIST_FIXTURE GH_SEARCH_FIXTURE || true
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]
  [ "$(count_calls 'issue list')" -eq 0 ]
}

# ── REQ-06 read-after-write lag: dedup holds even when gh list omits the
#    just-created issue ──────────────────────────────────────────────────

@test "dedup survives read-after-write lag: gh list never reflects the created issue, still zero duplicate creates" {
  fresh_env lag
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"; export GH_LIST_FIXTURE
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]

  : > "$GH_CALL_LOG"
  # gh list/search fixtures stay EMPTY on rerun -- the created issue never
  # appears (index lag). Local ledger dedup must still hold.
  analyze_h --digest "$FIXTURES/single-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
}
