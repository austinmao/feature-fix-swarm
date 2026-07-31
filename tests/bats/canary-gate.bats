#!/usr/bin/env bats
# canary-gate.sh — fail-closed browser-QA gate: web-touching diffs must
# carry a fresh, passing Canary results.json or the gate FAILs (never a
# silent skip). Asserts: WEB-TOUCH detection, kill-switch, missing-results
# fail-closed, pass/fail summary gating, staleness + its explicit bypass,
# usage error.

SCRIPT_REL="scripts/gsd/canary-gate.sh"

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SCRIPT="$ROOT/$SCRIPT_REL"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
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
  cat > "$1" <<EOF
{"status":"$2","summary":{"stepsTotal":5,"stepsPassed":5,"stepsFailed":0,"consoleErrors":$3,"networkFailures":$4},"steps":[]}
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
