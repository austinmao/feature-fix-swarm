#!/usr/bin/env bats
# plan-wall.sh keys review:wall:<phase> on the spec identity (gates.py spec-id),
# so a new drive / GSD_RUN_ID name gets no fresh wall allowance.

bats_require_minimum_version 1.5.0

setup() {
  LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/plan-wall.sh"
  REAL_GATES_PY="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/lib/gates.py"
  REAL_SCHEMA="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/schemas/review-finding.schema.json"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/packages/feature-fix-swarm/lib" "$REPO/schemas" "$REPO/bin"
  cp "$REAL_GATES_PY" "$REPO/packages/feature-fix-swarm/lib/gates.py"
  cp "$REAL_SCHEMA" "$REPO/schemas/review-finding.schema.json"
  cd "$REPO"
  git init -q -b 123-x
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  mkdir -p .planning/phases/1-foo
  echo "Phase 1: build a plain widget, nothing sensitive here" > .planning/phases/1-foo/PLAN.md
  export PATH="$REPO/bin:$PATH"
  export FFS_ADVERSARY_MODEL_PROBE=off
  export GATES_PY="$REPO/packages/feature-fix-swarm/lib/gates.py"
  export GATES_STORE="$BATS_TEST_TMPDIR/evidence.json"
  export ADVERSARY_BIN_CODEX=nonexistent-codex-binary-xyz
  export ADVERSARY_BIN_CLAUDE=nonexistent-claude-binary-xyz
  unset FFS_SPEC_ID
}

@test "two different GSD_RUN_ID values on branch 123-x share one review:wall allowance" {
  GSD_RUN_ID=drive-alpha run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -ne 3 ]
  GSD_RUN_ID=drive-alpha run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 3 ]
  # a renamed run must not get a fresh wall allowance
  GSD_RUN_ID=drive-beta run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 3 ]
  [[ "$output" == *"WALL-ROUND-CAP"* ]]
}

@test "wall counter is stored under the spec id, not the run id" {
  GSD_RUN_ID=drive-alpha run bash "$LEVER" .planning/phases/1-foo
  python3 - "$GATES_STORE" <<'PY'
import json, sys
loops = json.load(open(sys.argv[1]))["_loops"]
assert "spec-123" in loops, sorted(loops)
assert "drive-alpha" not in loops, sorted(loops)
PY
}
