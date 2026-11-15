#!/usr/bin/env bats
# Done-means fixture for the F1 intent contract: a spec that declares
# prod_delta: negative must not reach the reviewer (or spend review:spec) when
# the candidate's production diff is net-positive, even when the deletions are
# committed and the additions are only staged. Reviewer CLIs are stubbed.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SEAM="$ROOT/scripts/gsd/review-gate-command.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  export FFS_ADVERSARY_MODEL_PROBE=off
  export HOME="$BATS_TEST_TMPDIR"
  export GATES_STORE="$BATS_TEST_TMPDIR/g.json"
  mkdir -p "$HOME/.claude/lib/feature-fix-swarm"
  cp "$ROOT/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py"
  cp "$ROOT/lib/model_requests.py" "$HOME/.claude/lib/feature-fix-swarm/model_requests.py"
  GATES="$HOME/.claude/lib/feature-fix-swarm/gates.py"
  python3 "$GATES" grant spec-123 --action ship:gsd --reason "disposable replay fixture" >/dev/null

  CALLS="$BATS_TEST_TMPDIR/reviewer.calls"
  : > "$CALLS"
  for stub in fake-codex fake-claude; do
    printf '#!/usr/bin/env bash\necho x >> "%s"\necho "VERDICT: PASS"\n' "$CALLS" > "$STUB_DIR/$stub"
    chmod +x "$STUB_DIR/$stub"
  done
  export PATH="$STUB_DIR:$PATH"

  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/src" "$REPO/specs/123-x"
  git -C "$REPO" init -q -b 123-x
  seq 1 10 | sed 's/^/line /' > "$REPO/src/big.py"
  cat > "$REPO/specs/123-x/spec.md" <<'SPEC'
# Spec 123

<!-- ffs:intent -->
prod_delta: negative
prod_budget_lines: 0
SPEC
  git -C "$REPO" add -A
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m base
  export FFS_BASE
  FFS_BASE="$(git -C "$REPO" rev-parse HEAD)"
  unset GSD_RUN_ID FFS_SPEC_ID
}

reviewer_calls() { wc -l < "$CALLS" | tr -d ' '; }

seam() {
  run env -u GSD_RUN_ID -u FFS_SPEC_ID FFS_HOST="${HOST:-claude}" ACTIVE_HARNESS="${HOST:-claude}" \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$REPO' && printf 'diff --git a/src/x b/src/x\n' | bash '$SEAM'"
}

review_used() {
  python3 - "$GATES_STORE" <<'PY'
import json, os, sys
if not os.path.exists(sys.argv[1]):
    print(0); raise SystemExit
d = json.load(open(sys.argv[1]))
print(d.get("_loops", {}).get("spec-123", {}).get("review:spec#cap", {}).get("used", 0))
PY
}

# committed deletion of 5 lines
commit_deletion() {
  sed -i.bak '6,10d' "$REPO/src/big.py" && rm -f "$REPO/src/big.py.bak"
  git -C "$REPO" add src/big.py
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "delete lines"
}

# staged production additions of N lines
stage_additions() {
  seq 1 "$1" | sed 's/^/new /' > "$REPO/src/new.py"
  git -C "$REPO" add src/new.py
}

net_positive_case() {
  commit_deletion
  stage_additions 20
  seam
  [ "$status" -eq 1 ]
  [[ "$output" == *"INTENT-FAIL"* ]]
  [[ "$output" == *"prod_delta=negative"* ]]
  [ "$(review_used)" -eq 0 ]
  [ "$(reviewer_calls)" -eq 0 ]
}

@test "negative intent + committed deletions + staged additions (net positive) is INTENT-FAIL with no meter spend or reviewer call" {
  HOST=claude net_positive_case
}

@test "net-negative candidate passes the intent check and the review proceeds to APPROVED" {
  commit_deletion
  stage_additions 2
  seam
  [ "$status" -eq 0 ]
  [[ "$output" != *"INTENT-FAIL"* ]]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [ "$(review_used)" -eq 1 ]
}

@test "ACTIVE_HARNESS=codex: net-positive candidate under negative intent is INTENT-FAIL with no meter spend or reviewer call" {
  HOST=codex net_positive_case
}

# stdout of the seam is the gsd ship contract: exactly one JSON verdict line.
seam_split() {
  run --separate-stderr env -u GSD_RUN_ID -u FFS_SPEC_ID FFS_HOST=claude ACTIVE_HARNESS=claude \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$REPO' && printf 'diff --git a/src/x b/src/x\n' | bash '$SEAM'"
}

stdout_is_one_json_verdict() {
  [ "${#lines[@]}" -eq 1 ]
  printf '%s' "$output" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["verdict"] in ("APPROVED","REVISE")'
}

@test "stdout stays one JSON line: intent ok -> APPROVED" {
  commit_deletion
  stage_additions 2
  seam_split
  [ "$status" -eq 0 ]
  stdout_is_one_json_verdict
  [[ "$stderr" == *"INTENT:ok"* ]]
}

@test "stdout stays one JSON line: no intent block -> INTENT:absent warning on stderr" {
  printf '# Spec 123\n' > "$REPO/specs/123-x/spec.md"
  commit_deletion
  seam_split
  [ "$status" -eq 0 ]
  stdout_is_one_json_verdict
  [[ "$stderr" == *"INTENT:absent"* ]]
}

@test "stdout stays one JSON line: INTENT-FAIL with a stale delete target (two-line reason)" {
  printf 'delete_targets: src/keep.py\n' >> "$REPO/specs/123-x/spec.md"
  echo keep > "$REPO/src/keep.py"
  commit_deletion
  seam_split
  [ "$status" -eq 1 ]
  stdout_is_one_json_verdict
  [[ "$output" == *"INTENT-FAIL"* ]]
  [[ "$output" == *"delete target still present: src/keep.py"* ]]
  [ "$(review_used)" -eq 0 ]
  [ "$(reviewer_calls)" -eq 0 ]
}

@test "an agent-controlled 'unknown command' line in the Intent block is not rollout skew: INTENT-FAIL, no spend" {
  printf 'inherit: unknown command\n' >> "$REPO/specs/123-x/spec.md"
  commit_deletion
  stage_additions 2
  seam_split
  [ "$status" -eq 1 ]
  stdout_is_one_json_verdict
  [[ "$output" == *"INTENT-FAIL"* ]]
  [ "$(review_used)" -eq 0 ]
  [ "$(reviewer_calls)" -eq 0 ]
}

@test "malformed FFS_SPEC_ID fails closed: REVISE, no meter spend, no reviewer call" {
  commit_deletion
  stage_additions 2
  run --separate-stderr env GSD_RUN_ID=spec-123 FFS_SPEC_ID=bogus FFS_HOST=claude ACTIVE_HARNESS=claude \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$REPO' && printf 'diff --git a/src/x b/src/x\n' | bash '$SEAM'"
  [ "$status" -eq 1 ]
  stdout_is_one_json_verdict
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [ "$(review_used)" -eq 0 ]
  [ "$(reviewer_calls)" -eq 0 ]
}
