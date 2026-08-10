#!/usr/bin/env bats
# canary-gate.sh — fail-closed browser-QA gate: web-touching diffs must
# carry a fresh, passing Canary results.json or the gate FAILs (never a
# silent skip). Asserts: WEB-TOUCH detection, kill-switch, missing-results
# fail-closed, pass/fail summary gating, staleness + its explicit bypass,
# usage error.

SCRIPT_REL="scripts/gsd/canary-gate.sh"

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
  # canary-gate.sh resolves its waiver-record.sh sibling via its OWN
  # $BASH_SOURCE dirname, which is script-relative and ignores cwd/GATES_STORE
  # — running $ROOT's copy would make CANARY_GATE=off write into the
  # developer's real canonical evidence store no matter what this fixture's
  # git repo looks like. Run a fixture-local copy instead (see WR-140).
  mkdir -p "$REPO/scripts/gsd" "$REPO/lib"
  cp "$ROOT/scripts/gsd/canary-gate.sh" "$REPO/scripts/gsd/canary-gate.sh"
  cp "$ROOT/scripts/gsd/waiver-record.sh" "$REPO/scripts/gsd/waiver-record.sh"
  cp "$ROOT/lib/gates.py" "$REPO/lib/gates.py"
  chmod +x "$REPO/scripts/gsd/canary-gate.sh" "$REPO/scripts/gsd/waiver-record.sh"
  SCRIPT="$REPO/scripts/gsd/canary-gate.sh"
  cd "$REPO" || return 1
  git init -q
  git config user.email t@t
  git config user.name t
  echo "init" > README.md
  git add README.md
  git commit -q -m init
  BASE_SHA="$(git rev-parse HEAD)"
}

add_nonweb_commit() {
  mkdir -p docs
  echo "note" > docs/notes.md
  git add docs/notes.md
  git commit -q -m "docs change"
}

add_web_commit() {
  mkdir -p web/app
  echo "export default function Page(){}" > web/app/page.tsx
  git add web/app/page.tsx
  git commit -q -m "web change"
}

write_results() { # $1=path $2=status $3=consoleErrors $4=networkFailures
  # createdAt/endedAt mirror the real @usecanary/cli results.json shape —
  # REQ-301 records them verbatim, and their absence fails the gate closed.
  cat > "$1" <<EOF
{"status":"$2","createdAt":"2026-06-13T08:43:44.814Z","endedAt":"2026-06-13T08:48:02.991Z","summary":{"stepsTotal":5,"stepsPassed":5,"stepsFailed":0,"consoleErrors":$3,"networkFailures":$4},"steps":[]}
EOF
}

@test "no web-touch in diff is NOT-NEEDED" {
  add_nonweb_commit
  run bash "$SCRIPT" --diff-base "$BASE_SHA"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: NOT-NEEDED (no web-touch in diff)"* ]]
}

@test "kill-switch CANARY_GATE=off skips even with web-touch" {
  add_web_commit
  CANARY_GATE=off run bash "$SCRIPT" --diff-base "$BASE_SHA"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: disabled (CANARY_GATE=off)"* ]]
}

@test "web-touch with no resolvable results FAILs closed" {
  add_web_commit
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$BATS_TEST_TMPDIR/does-not-exist.json"
  [ "$status" -eq 1 ]
  [[ "$output" == *"canary-gate: FAIL — web-touch diff but no canary results (run a canary session or set CANARY_RESULTS)"* ]]
}

@test "passing fresh results PASS" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: PASS (steps 5/5, consoleErrors 0, networkFailures 0)"* ]]
}

@test "status failed FAILs" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" failed 0 0
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"canary-gate: FAIL"* ]]
  [[ "$output" == *"status=failed"* ]]
}

@test "consoleErrors > 0 FAILs" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 2 0
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"consoleErrors=2"* ]]
}

@test "networkFailures > 0 FAILs" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 3
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"networkFailures=3"* ]]
}

@test "stale results (older than HEAD) FAILs" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  touch -t 202001010000 "$RESULTS"
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"stale canary results (older than HEAD)"* ]]
}

@test "CANARY_GATE_ALLOW_STALE=1 bypasses staleness only" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  touch -t 202001010000 "$RESULTS"
  CANARY_GATE_ALLOW_STALE=1 run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: PASS"* ]]
}

@test "bad flag is usage error exit 2" {
  run bash "$SCRIPT" --bogus-flag
  [ "$status" -eq 2 ]
}

@test "unresolvable diff base FAILs closed (no origin remote, no --diff-base)" {
  add_web_commit
  run bash "$SCRIPT"
  [ "$status" -eq 1 ]
  # The default base now resolves via base-branch.sh, so the branch NAME in the
  # message follows the fixture host's git init.defaultBranch — pin the shape,
  # not the name (CI's default differed from the author machine's).
  [[ "$output" == *"canary-gate: FAIL — diff base 'origin/"* ]]
  [[ "$output" == *"unresolvable (fetch it or pass --diff-base)"* ]]
}

@test "--diff-base with no value is usage error exit 2" {
  run bash "$SCRIPT" --diff-base
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: canary-gate.sh"* ]]
}

@test "CANARY_RESULTS env var resolves results with no positional arg" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  CANARY_RESULTS="$RESULTS" run bash "$SCRIPT" --diff-base "$BASE_SHA"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: PASS"* ]]
}

@test "garbage stat output FAILs closed (nonnumeric mtime)" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  STATSTUB="$BATS_TEST_TMPDIR/statstub"
  mkdir -p "$STATSTUB"
  cat > "$STATSTUB/stat" <<'EOF'
#!/usr/bin/env bash
echo "  File: whatever"
echo "garbage multi-line output"
EOF
  chmod +x "$STATSTUB/stat"
  PATH="$STATSTUB:$PATH" run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"canary-gate: FAIL"* ]]
}

@test "status-only results with no summary counts FAILs closed" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  echo '{"status":"passed"}' > "$RESULTS"
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"canary-gate: FAIL"* ]]
}

@test "zero stepsTotal FAILs closed" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  cat > "$RESULTS" <<'EOF'
{"status":"passed","summary":{"stepsTotal":0,"stepsPassed":0,"stepsFailed":0,"consoleErrors":0,"networkFailures":0},"steps":[]}
EOF
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"canary-gate: FAIL"* ]]
}

@test "non-ASCII web filename still detected as web-touch (quotePath)" {
  # root-level so only the \.tsx$ anchor can match — C-quoted "\303\251.tsx"
  # ends in a quote character and evades it without core.quotePath=false
  echo "export default function Page(){}" > "é.tsx"
  git add "é.tsx"
  git commit -q -m "unicode web change"
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$BATS_TEST_TMPDIR/does-not-exist.json"
  [ "$status" -eq 1 ]
  [[ "$output" == *"web-touch diff but no canary results"* ]]
}

@test "jq missing FAILs closed" {
  MINPATH="$BATS_TEST_TMPDIR/no-jq-path"
  mkdir -p "$MINPATH"
  for bin in bash git grep cat stat sed; do
    b="$(command -v "$bin" 2>/dev/null)"
    [ -n "$b" ] && ln -sf "$b" "$MINPATH/$bin"
  done
  if PATH="$MINPATH" command -v jq >/dev/null 2>&1; then
    skip "jq still resolvable with a minimal PATH on this machine — can't simulate absence cleanly"
  fi
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  PATH="$MINPATH" run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"jq required to parse canary results"* ]]
}

@test "shell hook under scripts/hooks/ is NOT web-touch (WEB_EXCLUDE)" {
  mkdir -p scripts/hooks
  echo '#!/usr/bin/env bash' > scripts/hooks/path-guard.sh
  git add scripts/hooks/path-guard.sh
  git commit -q -m "shell hook change"
  run bash "$SCRIPT" --diff-base "$BASE_SHA"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: NOT-NEEDED (no web-touch in diff)"* ]]
}

@test "workflow YAML under a templates/ dir is NOT web-touch (WEB_EXCLUDE)" {
  mkdir -p templates/ci
  echo 'name: ffs-pr-fast' > templates/ci/pr-fast.yml
  git add templates/ci/pr-fast.yml
  git commit -q -m "ci template change"
  run bash "$SCRIPT" --diff-base "$BASE_SHA"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: NOT-NEEDED (no web-touch in diff)"* ]]
}

@test "plain .ts under hooks/ dir is still web-touch after WEB_EXCLUDE" {
  mkdir -p src/hooks
  echo "export const useCart = () => {}" > src/hooks/useCart.ts
  git add src/hooks/useCart.ts
  git commit -q -m "react hook change"
  run bash "$SCRIPT" --diff-base "$BASE_SHA" "$BATS_TEST_TMPDIR/does-not-exist.json"
  [ "$status" -eq 1 ]
  [[ "$output" == *"web-touch diff but no canary results"* ]]
}

# ── spec-008 Phase 3 (REQ-301, AC-004): typed canary evidence ────────────────
# A passing run must durably record {run_id, sha, pass, created_at, ended_at,
# ts} BEFORE printing PASS; sha comes from `git rev-parse HEAD` in this
# trusted wrapper (C6 — never from results.json), captured at entry and
# re-verified after the completeness check. Fail-closed: unrecordable
# evidence or mid-run HEAD drift refuses PASS.

@test "AC-004: PASS writes one typed canary record" {
  add_web_commit
  HEAD_SHA="$(git rev-parse HEAD)"
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  STORE="$BATS_TEST_TMPDIR/evidence.json"
  GATES_STORE="$STORE" GSD_RUN_ID="run-42" run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: PASS"* ]]
  [ "$(jq -r '.canary | length' "$STORE")" = "1" ]
  [ "$(jq -r '.canary[0].run_id' "$STORE")" = "run-42" ]
  [ "$(jq -r '.canary[0].sha' "$STORE")" = "$HEAD_SHA" ]
  [ "$(jq -r '.canary[0].pass' "$STORE")" = "true" ]
  [ "$(jq -r '.canary[0].created_at' "$STORE")" = "2026-06-13T08:43:44.814Z" ]
  [ "$(jq -r '.canary[0].ended_at' "$STORE")" = "2026-06-13T08:48:02.991Z" ]
  [ "$(jq -r '.canary[0].ts | type' "$STORE")" = "number" ]
}

@test "AC-004: record write failure fails the gate (no PASS)" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  RO="$BATS_TEST_TMPDIR/ro"
  mkdir -p "$RO"
  chmod 555 "$RO"
  GATES_STORE="$RO/evidence.json" run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  chmod 755 "$RO"
  [ "$status" -eq 1 ]
  [[ "$output" == *"CANARY-EVIDENCE-UNRECORDED"* ]]
  [[ "$output" != *"canary-gate: PASS"* ]]
}

@test "AC-004: absent GSD_RUN_ID records unattributed" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  STORE="$BATS_TEST_TMPDIR/evidence.json"
  GATES_STORE="$STORE" run env -u GSD_RUN_ID bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 0 ]
  [[ "$output" == *"canary-gate: PASS"* ]]
  [ "$(jq -r '.canary[0].run_id' "$STORE")" = "unattributed" ]
}

@test "AC-004: mid-run HEAD drift refuses with no record" {
  add_web_commit
  RESULTS="$BATS_TEST_TMPDIR/results.json"
  write_results "$RESULTS" passed 0 0
  STORE="$BATS_TEST_TMPDIR/evidence.json"
  REALGIT="$(command -v git)"
  STUB="$BATS_TEST_TMPDIR/gitstub"
  COUNT="$BATS_TEST_TMPDIR/rev-parse-head-count"
  mkdir -p "$STUB"
  # First bare `git rev-parse HEAD` (gate-entry capture) passes through to
  # real git; the second (post-completeness re-verify) returns a different
  # sha — simulating a commit landing mid-run. All other git calls pass
  # through untouched.
  cat > "$STUB/git" <<EOF
#!/usr/bin/env bash
if [ "\$#" -eq 2 ] && [ "\$1" = "rev-parse" ] && [ "\$2" = "HEAD" ]; then
  C="\$(cat "$COUNT" 2>/dev/null || echo 0)"
  echo "\$((C + 1))" > "$COUNT"
  if [ "\$C" -ge 1 ]; then
    echo "$(printf '0%.0s' {1..40})"
    exit 0
  fi
fi
exec "$REALGIT" "\$@"
EOF
  chmod +x "$STUB/git"
  GATES_STORE="$STORE" PATH="$STUB:$PATH" run bash "$SCRIPT" --diff-base "$BASE_SHA" "$RESULTS"
  [ "$status" -eq 1 ]
  [[ "$output" == *"CANARY-IDENTITY-DRIFT"* ]]
  [[ "$output" != *"canary-gate: PASS"* ]]
  if [ -f "$STORE" ]; then
    [ "$(jq -r '.canary // [] | length' "$STORE")" = "0" ]
  fi
}
