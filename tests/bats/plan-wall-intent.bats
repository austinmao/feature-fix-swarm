#!/usr/bin/env bats
# plan-wall.sh runs intent-check --stage plan in its admission step: first run
# pins the spec's intent block, a later edit of the block fails closed with
# INTENT-FAIL before any reviewer call or review:wall:* spend.

bats_require_minimum_version 1.5.0

setup() {
  LEVER="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/scripts/gsd/plan-wall.sh"
  REAL_GATES_PY="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/lib/gates.py"
  REAL_SCHEMA="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)/schemas/review-finding.schema.json"
  REPO="$BATS_TEST_TMPDIR/repo"
  mkdir -p "$REPO/packages/feature-fix-swarm/lib" "$REPO/schemas" "$REPO/bin" "$REPO/specs/123-x"
  cp "$REAL_GATES_PY" "$REPO/packages/feature-fix-swarm/lib/gates.py"
  cp "$REAL_SCHEMA" "$REPO/schemas/review-finding.schema.json"
  cd "$REPO"
  git init -q -b 123-x
  git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
  mkdir -p .planning/phases/1-foo
  echo "Phase 1: build a plain widget, nothing sensitive here" > .planning/phases/1-foo/PLAN.md
  cat > specs/123-x/spec.md <<'SPEC'
# Spec 123

<!-- ffs:intent -->
prod_delta: negative
prod_budget_lines: 0
SPEC
  CALLS="$BATS_TEST_TMPDIR/reviewer.calls"
  : > "$CALLS"
  for stub in fake-codex fake-claude; do
    printf '#!/usr/bin/env bash\necho x >> "%s"\nexit 1\n' "$CALLS" > "$REPO/bin/$stub"
    chmod +x "$REPO/bin/$stub"
  done
  export PATH="$REPO/bin:$PATH"
  export FFS_ADVERSARY_MODEL_PROBE=off
  export GATES_PY="$REPO/packages/feature-fix-swarm/lib/gates.py"
  export GATES_STORE="$BATS_TEST_TMPDIR/g.json"
  export ADVERSARY_BIN_CODEX=fake-codex
  export ADVERSARY_BIN_CLAUDE=fake-claude
  unset FFS_SPEC_ID
}

reviewer_calls() { wc -l < "$CALLS" | tr -d ' '; }

wall_spend() {
  python3 - "$GATES_STORE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
loops = d.get("_loops", {}).get("spec-123", {})
print(sum(v.get("used", 0) for k, v in loops.items() if k.startswith("review:wall")))
PY
}

@test "plan wall pins the intent on first run; an edited intent block then fails INTENT-FAIL with no reviewer call and no new wall spend" {
  GSD_RUN_ID=drive-alpha run bash "$LEVER" .planning/phases/1-foo
  [[ "$output" != *"INTENT-FAIL"* ]]
  python3 - "$GATES_STORE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert "spec-123" in d.get("_intent", {}), sorted(d)
PY
  calls_before="$(reviewer_calls)"
  spend_before="$(wall_spend)"

  sed -i.bak 's/prod_delta: negative/prod_delta: positive/' specs/123-x/spec.md && rm -f specs/123-x/spec.md.bak
  GSD_RUN_ID=drive-alpha run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -ne 0 ]
  [[ "$output" == *"INTENT-FAIL"* ]]
  [ "$(reviewer_calls)" -eq "$calls_before" ]
  [ "$(wall_spend)" -eq "$spend_before" ]
}

@test "an agent-controlled 'unknown command' Intent line is not rollout skew: admission refused (78), no reviewer call" {
  printf 'inherit: unknown command\n' >> specs/123-x/spec.md
  GSD_RUN_ID=drive-alpha run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 78 ]
  [[ "$output" == *"INTENT-FAIL"* ]]
  [ "$(reviewer_calls)" -eq 0 ]
}

@test "malformed FFS_SPEC_ID fails closed at admission: exit 78, no reviewer call, no wall spend" {
  FFS_SPEC_ID=bogus GSD_RUN_ID=drive-alpha run bash "$LEVER" .planning/phases/1-foo
  [ "$status" -eq 78 ]
  [ "$(reviewer_calls)" -eq 0 ]
  [ ! -f "$GATES_STORE" ] || [ "$(wall_spend)" -eq 0 ]
}
