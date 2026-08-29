#!/usr/bin/env bats
bats_require_minimum_version 1.5.0

# retro-aggregate.bats — GH-114 RED acceptance: retro filing must be
# proportional to distinct fingerprints, not digest rows, and a filed digest
# file must be consumed (never re-read) with oldest-first drain order.
# Authored following the setup() shape and helper contract of
# tests/bats/retro-e2e.bats / tests/bats/helpers/retro-shims.bash. Asserts
# only through the gh PATH shim's call/body logs and the retro-ledger.jsonl
# file — never by importing lib/retro_state.py — so these stay black-box
# over the same boundary run-finalizer.sh uses.
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

# analyze_h [extra retro.sh analyze args] -- isolated-HOME + matching
# --state-root wrapper, mirrors retro-e2e.bats.
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

count_calls() { # count_calls <prefix>
  local n
  n="$(grep -c "^$1" "$GH_CALL_LOG" 2>/dev/null)"
  printf '%s' "${n:-0}"
}

pending_digests() { # pending_digests -- count of un-consumed dated digest files
  find "$REPO/.feature-fix-swarm" -maxdepth 1 -type f -name 'digest-*.jsonl' -print 2>/dev/null | wc -l | tr -d ' '
}

# ── Test A: aggregation — N rows, one fingerprint, one write ────────────────

@test "aggregation: 5 rows sharing one fingerprint produce exactly 1 create carrying occurrences:5" {
  fresh_env aggA
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_SEARCH_FIXTURE="$FIXTURES/gh-list-empty.json"
  export GH_LIST_FIXTURE GH_SEARCH_FIXTURE

  analyze_h --digest "$FIXTURES/repeat-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]
  [ "$(count_calls 'issue comment')" -eq 0 ]

  BODY="$GH_CREATE_LOG/create-1.body"
  [ -f "$BODY" ]
  grep -Eq '<!-- ffs-retro fingerprint:6f8560bdd33c6f56 priority:P1 occurrences:5 -->' "$BODY"
}

# ── Test B: consumed sink — a filed digest is marked consumed, never re-read ─

@test "consumed sink: an auto-discovered digest is filed once then marked consumed, rerun performs zero writes" {
  fresh_env aggB
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_SEARCH_FIXTURE="$FIXTURES/gh-list-empty.json"
  export GH_LIST_FIXTURE GH_SEARCH_FIXTURE

  DATED="$REPO/.feature-fix-swarm/digest-20260810.jsonl"
  cp "$FIXTURES/repeat-p1-digest.jsonl" "$DATED"

  analyze_h
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 1 ]
  [ "$(count_calls 'issue comment')" -eq 0 ]
  [ ! -f "$DATED" ]
  [ ! -f "$DATED.processing" ]
  [ -f "$DATED.consumed" ]
  [ "$(pending_digests)" -eq 0 ]

  : > "$GH_CALL_LOG"
  analyze_h
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 0 ]
  [[ "$output" == *"RETRO:no-events"* ]]
}

# ── Test C: drain order — older digest processed before newer, never skipped ─

@test "drain order: the OLDER dated digest is filed before a NEWER one, never shadowed" {
  fresh_env aggC
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_SEARCH_FIXTURE="$FIXTURES/gh-list-empty.json"
  export GH_LIST_FIXTURE GH_SEARCH_FIXTURE

  OLDER="$REPO/.feature-fix-swarm/digest-20260810.jsonl"
  NEWER="$REPO/.feature-fix-swarm/digest-20260828.jsonl"
  cp "$FIXTURES/repeat-p1-digest.jsonl" "$OLDER"
  cp "$FIXTURES/safe-digest.jsonl" "$NEWER"

  analyze_h
  [ "$status" -eq 0 ]
  [ ! -f "$OLDER" ]
  [ -f "$NEWER" ]
  BODY1="$GH_CREATE_LOG/create-1.body"
  [ -f "$BODY1" ]
  grep -q 'fingerprint:6f8560bdd33c6f56' "$BODY1"

  : > "$GH_CALL_LOG"
  analyze_h
  [ "$status" -eq 0 ]
  [ ! -f "$NEWER" ]
  [ "$(pending_digests)" -eq 0 ]
}

# ── Test D: inflated ledger tolerance — a pre-fix one-row-per-occurrence
#    ledger is read without crashing or going backwards ───────────────────

@test "inflated ledger: a pre-fix one-row-per-occurrence ledger sums as 40 and the next run reaches 45" {
  fresh_env aggD
  GH_LIST_FIXTURE="$FIXTURES/gh-list-exact-single.json"
  GH_SEARCH_FIXTURE="$FIXTURES/gh-list-exact-single.json"
  export GH_LIST_FIXTURE GH_SEARCH_FIXTURE

  analyze_h --digest "$FIXTURES/repeat-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]

  LEDGER="$RETRO_STATE/retro-ledger.jsonl"
  [ -f "$LEDGER" ]

  FIRST_COMMENT="$GH_COMMENT_LOG/comment-1.body"
  [ -f "$FIRST_COMMENT" ]
  run python3 - "$FIRST_COMMENT" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read()
match = re.search(rb"occurrences:([0-9]+)", raw)
assert match, raw
print(int(match.group(1)))
PY
  [ "$status" -eq 0 ]
  first_count="$output"
  [ "$first_count" -eq 5 ]

  # Simulate a pre-fix ledger: take the comment row for this fingerprint,
  # drop its aggregate count key, and re-emit it 40 times (mode 600) so the
  # ledger looks like it was written one row per occurrence.
  run python3 - "$LEDGER" <<'PY'
import json, sys
path = sys.argv[1]
rows = [json.loads(l) for l in open(path) if l.strip()]
comment_rows = [r for r in rows if r.get("action") == "comment"]
assert comment_rows, rows
row = dict(comment_rows[-1])
fingerprint = row["fingerprint"]
row.pop("count", None)
with open(path, "w") as fh:
    for _ in range(40):
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
print(fingerprint)
PY
  [ "$status" -eq 0 ]
  fingerprint="$output"
  chmod 600 "$LEDGER"

  : > "$GH_CALL_LOG"
  analyze_h --digest "$FIXTURES/repeat-p1-digest.jsonl"
  [ "$status" -eq 0 ]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 1 ]

  SECOND_COMMENT="$GH_COMMENT_LOG/comment-2.body"
  [ -f "$SECOND_COMMENT" ]
  grep -q "fingerprint:$fingerprint" "$SECOND_COMMENT"
  run python3 - "$SECOND_COMMENT" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read()
match = re.search(rb"occurrences:([0-9]+)", raw)
assert match, raw
print(int(match.group(1)))
PY
  [ "$status" -eq 0 ]
  second_count="$output"
  [ "$second_count" -eq 45 ]
  [ "$second_count" -gt "$first_count" ]
}

# ── Test E: snapshot-first claim — filing failure leaves .processing, never
#    the bare name and never .consumed; a re-run does not re-file it ───────

@test "snapshot-first: a filing failure leaves the claimed digest at .processing, not consumed, and it is never re-filed" {
  fresh_env aggE
  GH_LIST_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_SEARCH_FIXTURE="$FIXTURES/gh-list-empty.json"
  GH_WRITE_FAIL_CODE=500  # an unrecognized status -> fatal write-failure
  export GH_LIST_FIXTURE GH_SEARCH_FIXTURE GH_WRITE_FAIL_CODE

  DATED="$REPO/.feature-fix-swarm/digest-20260812.jsonl"
  cp "$FIXTURES/single-p1-digest.jsonl" "$DATED"

  analyze_h
  [ "$status" -ne 0 ]
  [ ! -f "$DATED" ]
  [ ! -f "$DATED.consumed" ]
  [ ! -f "$DATED.rejected" ]
  [ -f "$DATED.processing" ]
  [[ "$stderr" == *"RETRO:filing-failed-digest-left-processing"* ]]
  [[ "$stderr" == *"$DATED.processing"* ]]

  unset GH_WRITE_FAIL_CODE
  : > "$GH_CALL_LOG"
  analyze_h
  [ "$status" -eq 0 ]
  [[ "$output" == *"RETRO:no-events"* ]]
  [ "$(count_calls 'issue create')" -eq 0 ]
  [ "$(count_calls 'issue comment')" -eq 0 ]
  [ -f "$DATED.processing" ]
  [ "$(pending_digests)" -eq 0 ]
}
