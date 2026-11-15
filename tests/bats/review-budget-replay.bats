#!/usr/bin/env bats
# Candidate identity is computed by gates.py from production content (merge-base
# FFS_BASE..working tree incl. untracked, minus tests/docs/specs), so each
# "candidate" here is a real production file in the repo under test.
# Done-means fixture for the single review meter (review:spec, max 2): an
# agent run must not be able to turn a spent review budget into another
# round, whichever site (skill or ship seam) spends it and however the run id
# is spelled. Reviewer CLIs are stubbed; every case uses its own GATES_STORE.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  SEAM="$ROOT/scripts/gsd/review-gate-command.sh"
  STUB_DIR="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUB_DIR"
  export FFS_ADVERSARY_MODEL_PROBE=off
  export HOME="$BATS_TEST_TMPDIR"
  export GATES_STORE="$BATS_TEST_TMPDIR/review-budget-store.json"
  mkdir -p "$HOME/.claude/lib/feature-fix-swarm"
  cp "$ROOT/lib/gates.py" "$HOME/.claude/lib/feature-fix-swarm/gates.py"
  cp "$ROOT/lib/model_requests.py" "$HOME/.claude/lib/feature-fix-swarm/model_requests.py"
  GATES="$HOME/.claude/lib/feature-fix-swarm/gates.py"
  for rid in spec-123 spec-123-p4 drive-fresh-1; do
    python3 "$GATES" grant "$rid" --action ship:gsd --reason "disposable replay fixture" >/dev/null
  done

  CALLS="$BATS_TEST_TMPDIR/reviewer.calls"
  : > "$CALLS"
  for stub in fake-codex fake-claude; do
    printf '#!/usr/bin/env bash\necho x >> "%s"\necho "VERDICT: PASS"\n' "$CALLS" > "$STUB_DIR/$stub"
    chmod +x "$STUB_DIR/$stub"
  done
  export PATH="$STUB_DIR:$PATH"

  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO"
  git -C "$REPO" init -q -b 123-x
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  export FFS_BASE
  FFS_BASE="$(git -C "$REPO" rev-parse HEAD)"
  unset GSD_RUN_ID FFS_SPEC_ID
}

reviewer_calls() { wc -l < "$CALLS" | tr -d ' '; }

# cand <dir> <name>: make a new production candidate in <dir>.
cand() { mkdir -p "$1/src"; printf 'print("%s")\n' "$2" > "$1/src/$2.py"; }

# seam <dir> <extra env assignments...>
seam() {
  local dir="$1"; shift
  run env -u GSD_RUN_ID -u FFS_SPEC_ID "$@" FFS_HOST="${HOST:-claude}" ACTIVE_HARNESS="${HOST:-claude}" \
    ADVERSARY_BIN_CODEX=fake-codex ADVERSARY_BIN_CLAUDE=fake-claude \
    bash -c "cd '$dir' && printf 'diff --git a/src/x b/src/x\n' | bash '$SEAM'"
}

# skill_spend: the review-gate skill's spend call (token computed by gates.py).
skill_spend() {
  run bash -c "cd '$REPO' && SID=\"\$(python3 '$GATES' spec-id)\" && python3 '$GATES' loop-round \"\$SID\" review:spec --max 2 --durable --site skill"
}

used() {
  python3 - "$GATES_STORE" "${1:-spec-123}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["_loops"][sys.argv[2]]["review:spec#cap"]["used"])
PY
}

assert_no_lever_words() {
  ! printf '%s\n' "$output" | grep -Eiq 'grant|renew|reset|--max|another round'
}

assert_exhausted() {
  [ "$status" -ne 0 ]
  [[ "$output" == *"BUDGET-EXHAUSTED review:spec spec-123"* ]]
  [[ "$output" == *"/prompt-master"* ]]
  assert_no_lever_words
}

three_rounds() {
  cand "$REPO" a; seam "$REPO"
  [ "$status" -eq 0 ]
  cand "$REPO" b; skill_spend
  [ "$status" -eq 0 ]
  cand "$REPO" c; seam "$REPO"
  assert_exhausted
  [ "$(used)" -eq 2 ]
}

@test "replay under ACTIVE_HARNESS=claude: round 3 on a different candidate is BUDGET-EXHAUSTED with no lever words" {
  HOST=claude three_rounds
}

@test "replay under ACTIVE_HARNESS=codex: round 3 on a different candidate is BUDGET-EXHAUSTED with no lever words" {
  HOST=codex three_rounds
}

@test "skill-site spend is also refused over the cap with the same stop contract" {
  cand "$REPO" a; skill_spend; [ "$status" -eq 0 ]
  cand "$REPO" b; skill_spend; [ "$status" -eq 0 ]
  cand "$REPO" c; skill_spend
  assert_exhausted
}

@test "same candidate spent at skill then at the seam is one round" {
  cand "$REPO" same
  skill_spend; [ "$status" -eq 0 ]
  seam "$REPO"
  [ "$status" -eq 0 ]
  [[ "$output" == *'"verdict":"APPROVED"'* ]]
  [ "$(used)" -eq 1 ]
}

@test "docs-only edit committed between skill and seam is still the same candidate (one round)" {
  cand "$REPO" same
  skill_spend; [ "$status" -eq 0 ]
  mkdir -p "$REPO/docs"; echo notes > "$REPO/docs/x.md"
  git -C "$REPO" add docs/x.md
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -q -m "docs only"
  seam "$REPO"
  [ "$status" -eq 0 ]
  [ "$(used)" -eq 1 ]
}

@test "GSD_RUN_ID=spec-123-p4 shares the spec-123 allowance" {
  cand "$REPO" a; seam "$REPO"; [ "$status" -eq 0 ]
  cand "$REPO" b; skill_spend; [ "$status" -eq 0 ]
  cand "$REPO" c; seam "$REPO" GSD_RUN_ID=spec-123-p4
  assert_exhausted
  [ "$(used)" -eq 2 ]
}

@test "detached worktree with FFS_SPEC_ID=spec-123 and a new GSD_RUN_ID shares the allowance" {
  WT="$BATS_TEST_TMPDIR/wt"
  git -C "$REPO" worktree add -q --detach "$WT"
  cand "$REPO" a; seam "$REPO"; [ "$status" -eq 0 ]
  cand "$WT" b; seam "$WT" FFS_SPEC_ID=spec-123
  [ "$status" -eq 0 ]
  [ "$(used)" -eq 2 ]
  cand "$WT" c; seam "$WT" FFS_SPEC_ID=spec-123 GSD_RUN_ID=drive-fresh-1
  assert_exhausted
}

@test "exhausted spec is never approved at the seam, even with every finding resolved as issue; no reviewer call, used unchanged" {
  cand "$REPO" a; seam "$REPO"; [ "$status" -eq 0 ]
  cand "$REPO" b; skill_spend; [ "$status" -eq 0 ]
  sig="$(cd "$REPO" && python3 "$GATES" findings-queue add src/a.py "unbounded loop" --severity HIGH --spec spec-123 \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["sig"])')"
  (cd "$REPO" && python3 "$GATES" findings-queue resolve "$sig" --disposition issue --reason "tracked as follow-up issue")
  before="$(reviewer_calls)"
  cand "$REPO" c; seam "$REPO"
  assert_exhausted
  [[ "$output" != *'"verdict":"APPROVED"'* ]]
  [ "$(used)" -eq 2 ]
  [ "$(reviewer_calls)" -eq "$before" ]
}

@test "FFS_SPEC_ID that disagrees with the branch NNN: spec-id exits non-zero SPEC-ID:mismatch and the seam REVISEs without a reviewer call" {
  run bash -c "cd '$REPO' && FFS_SPEC_ID=spec-456 python3 '$GATES' spec-id"
  [ "$status" -ne 0 ]
  [[ "$output" == *"SPEC-ID:mismatch"* ]]
  cand "$REPO" a; seam "$REPO" FFS_SPEC_ID=spec-456
  [ "$status" -ne 0 ]
  [[ "$output" == *'"verdict":"REVISE"'* ]]
  [ "$(reviewer_calls)" -eq 0 ]
}
