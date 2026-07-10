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
