#!/usr/bin/env bats
# security-model-fence.sh — security-keyword planning fence tests

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  LEVER="$REPO_ROOT/scripts/gsd/security-model-fence.sh"
  TMP="$(mktemp -d)"
  mkdir -p "$TMP/.planning"
  cat > "$TMP/.planning/config.json" <<'JSON'
{
  "model_overrides": {
    "gsd-planner": "fable",
    "gsd-plan-checker": "fable",
    "gsd-executor": "sonnet",
    "gsd-verifier": "opus"
  }
}
JSON
}

teardown() { rm -rf "$TMP"; }

planner_model() { python3 -c "import json;print(json.load(open('$TMP/.planning/config.json'))['model_overrides']['$1'])"; }

@test "security keywords in ROADMAP -> planning roles fable->opus, executor untouched" {
  echo "Phase 1: harden RLS policies and JWT verification" > "$TMP/.planning/ROADMAP.md"
  run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"fable -> opus"* ]]
  [ "$(planner_model gsd-planner)" = "opus" ]
  [ "$(planner_model gsd-plan-checker)" = "opus" ]
  [ "$(planner_model gsd-executor)" = "sonnet" ]
}

@test "non-security ROADMAP -> config unchanged" {
  echo "Phase 1: add pagination to the blog listing page" > "$TMP/.planning/ROADMAP.md"
  run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"config unchanged"* ]]
  [ "$(planner_model gsd-planner)" = "fable" ]
}

@test "keyword only in extra spec file arg -> fence fires" {
  echo "Phase 1: build the widget" > "$TMP/.planning/ROADMAP.md"
  echo "Users authenticate with a password" > "$TMP/spec.md"
  run bash "$LEVER" "$TMP/.planning" "$TMP/spec.md"
  [ "$status" -eq 0 ]
  [ "$(planner_model gsd-planner)" = "opus" ]
}

@test "full model IDs (resolved config) also fenced" {
  python3 - "$TMP/.planning/config.json" <<'EOF'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg["model_overrides"]["gsd-planner"] = "claude-fable-5"
json.dump(cfg, open(p, "w"))
EOF
  echo "OAuth token rotation for the payment webhook" > "$TMP/.planning/ROADMAP.md"
  run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ "$(planner_model gsd-planner)" = "claude-opus-4-8" ]
}

@test "missing .planning -> fail-soft warn, exit 0" {
  run bash "$LEVER" "$TMP/nonexistent"
  [ "$status" -eq 0 ]
  [[ "$output" == *"fail-soft"* ]]
}

@test "no planning docs to scan -> warn, config unchanged, exit 0" {
  run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"config unchanged"* ]]
  [ "$(planner_model gsd-planner)" = "fable" ]
}
