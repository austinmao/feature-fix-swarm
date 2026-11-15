#!/usr/bin/env bats
# Runs the review-gate SKILL.md "Review budget" bash block in a fixture repo.

bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  export GATES_STORE="$BATS_TEST_TMPDIR/g.json"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/specs/123-x"
  git -C "$REPO" init -q -b 123-x
  printf '# S\n' > "$REPO/specs/123-x/spec.md"
  git -C "$REPO" add -A
  git -C "$REPO" -c user.email=t@t -c user.name=t commit -qm init
  BLOCK="$BATS_TEST_TMPDIR/block.sh"
  python3 - "$ROOT/skills/review-gate/SKILL.md" > "$BLOCK" <<'PY'
import re, sys
t = open(sys.argv[1]).read()
i = t.index("### Review budget")
print(re.search(r"```bash\n(.*?)\n```", t[i:], re.S).group(1))
PY
  OLD_GATES="$BATS_TEST_TMPDIR/old-gates.py"
  printf 'import sys\nprint("unknown command: " + sys.argv[1], file=sys.stderr)\nsys.exit(2)\n' > "$OLD_GATES"
  unset FFS_SPEC_ID GSD_RUN_ID
}

run_block() {
  run --separate-stderr env GATES_PY="$1" REPO_ROOT="$REPO" bash -c "cd '$REPO' && bash '$BLOCK'"
}

@test "an old gates.py without spec-id is rollout skew: the skill warns and runs unmetered" {
  run_block "$OLD_GATES"
  [ "$status" -eq 0 ]
  [[ "$stderr" == *"REVIEW-BUDGET:unmetered"* ]]
}

@test "a malformed FFS_SPEC_ID still fails closed" {
  FFS_SPEC_ID=bogus run_block "$ROOT/lib/gates.py"
  [ "$status" -eq 1 ]
  [[ "$output" == *"spec identity invalid"* ]]
}
