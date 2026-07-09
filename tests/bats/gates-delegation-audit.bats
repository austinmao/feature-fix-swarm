#!/usr/bin/env bats
# gates.py delegation-audit — advisory orchestrator-discipline lever.
# Asserts: spawn-model histogram, UNPINNED-BUILD + INLINE-MECHANICAL detection,
# sidechain/poll exclusion, WARN/OK verdicts, and the always-exit-0 contract.

GATES="lib/gates.py"

setup() {
  cd "$BATS_TEST_DIRNAME/../.."
  FIXTURE="$BATS_TEST_TMPDIR/transcript.jsonl"
  cat > "$FIXTURE" <<'EOF'
{"type":"mode","mode":"normal"}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Agent","input":{"model":"opus","description":"Adversarial review auth RLS"}}]}}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Agent","input":{"model":"sonnet","description":"Rebase 252 onto fresh main"}}]}}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Agent","input":{"description":"Rebase 260 onto fresh main"}}]}}
{"type":"assistant","isSidechain":true,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"git rebase main","description":"sub-agent rebase (fine)"}}]}}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"for f in a b c; do sed -i '' 's/x/y/' \"$f\"; done","description":"restamp loop"}}]}}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"git rebase --continue","description":"drain conflict"}}]}}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"git checkout --theirs .planning/ROADMAP.md","description":"take theirs"}}]}}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"for c in $(git log --format=%h); do git show $c:doc.md > out/$c.md; done","description":"doc extraction loop"}}]}}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"while true; do grep -q done seen.txt && break; sleep 30; done","description":"CI poll loop"}}]}}
not-json garbage line
EOF
  CLEAN="$BATS_TEST_TMPDIR/clean.jsonl"
  cat > "$CLEAN" <<'EOF'
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Agent","input":{"model":"haiku","description":"Scout branch state"}}]}}
{"type":"assistant","isSidechain":false,"message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":{"command":"gh run watch 12345","description":"CI watch"}}]}}
EOF
}

@test "histogram counts spawns by model, inherit for unpinned" {
  run python3 "$GATES" delegation-audit "$FIXTURE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"SPAWNS: 3"* ]]
  [[ "$output" == *"opus: 1"* ]]
  [[ "$output" == *"sonnet: 1"* ]]
  [[ "$output" == *"inherit: 1"* ]]
}

@test "unpinned build spawn flagged UNPINNED-BUILD" {
  run python3 "$GATES" delegation-audit "$FIXTURE"
  [[ "$output" == *"UNPINNED-BUILD: Rebase 260 onto fresh main"* ]]
  [[ "$output" == *"unpinned-build=1"* ]]
}

@test "main-loop trip-wires counted; sidechain and poll loops excluded" {
  run python3 "$GATES" delegation-audit "$FIXTURE"
  [[ "$output" == *"inline-mechanical=4"* ]]
  [[ "$output" != *"sub-agent rebase"* ]]
  [[ "$output" != *"CI poll loop"* ]]
}

@test "WARN verdict trips on unpinned-build or over-threshold, still exit 0" {
  run python3 "$GATES" delegation-audit "$FIXTURE"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELEGATION-WARN"* ]]
}

@test "clean transcript is DELEGATION-OK" {
  run python3 "$GATES" delegation-audit "$CLEAN"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELEGATION-OK"* ]]
}

@test "reads stdin when no path given" {
  run bash -c "python3 '$GATES' delegation-audit < '$CLEAN'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"DELEGATION-OK"* ]]
}

@test "--threshold raises the inline-mechanical bar (unpinned still warns)" {
  run python3 "$GATES" delegation-audit "$FIXTURE" --threshold 10
  [ "$status" -eq 0 ]
  [[ "$output" == *"threshold=10"* ]]
  [[ "$output" == *"DELEGATION-WARN"* ]]
}
