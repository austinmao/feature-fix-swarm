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

@test "extra spec file path containing whitespace is scanned as ONE file, not word-split" {
  echo "Phase 1: build the widget" > "$TMP/.planning/ROADMAP.md"
  mkdir -p "$TMP/specs with spaces"
  echo "Users authenticate with a password" > "$TMP/specs with spaces/spec.md"
  run bash "$LEVER" "$TMP/.planning" "$TMP/specs with spaces/spec.md"
  [ "$status" -eq 0 ]
  [ "$(planner_model gsd-planner)" = "opus" ]
}

@test "extra spec file path shaped like a grep option is treated as a path, never a flag" {
  echo "Phase 1: build the widget" > "$TMP/.planning/ROADMAP.md"
  # A RELATIVE file literally starting with "-e" must be scanned as a
  # filename, never parsed as grep's -e flag (spec-004 fix round finding
  # 14) — an absolute path never triggers this since it starts with "/".
  printf 'Users authenticate with a password\n' > "$TMP/-emergency-notes.md"
  run bash -c "cd '$TMP' && bash '$LEVER' .planning -emergency-notes.md"
  [ "$status" -eq 0 ]
  [ "$(planner_model gsd-planner)" = "opus" ]
}

@test "full model IDs (resolved config) also fenced" {
  python3 - "$TMP/.planning/config.json" <<'EOF'
import json, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg["model_overrides"]["gsd-planner"] = "claude-fable-5-1"
json.dump(cfg, open(p, "w"))
EOF
  echo "OAuth token rotation for the payment webhook" > "$TMP/.planning/ROADMAP.md"
  run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ "$(planner_model gsd-planner)" = "claude-opus-5-5" ]
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

# ── spec-004 AC-007: kill-switch + run-scoped marker ─────────────────────────

@test "SECURITY_MODEL_FENCE=off -> config unchanged, no marker, exit 0" {
  echo "Phase 1: harden RLS policies and JWT verification" > "$TMP/.planning/ROADMAP.md"
  SECURITY_MODEL_FENCE=off run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [[ "$output" == *"kill-switch"* ]]
  [ "$(planner_model gsd-planner)" = "fable" ]
  [ ! -d "$TMP/.planning/run-state" ]
}

@test "trigger writes a run-scoped marker under .planning/run-state/" {
  echo "Phase 1: harden RLS policies and JWT verification" > "$TMP/.planning/ROADMAP.md"
  GSD_RUN_ID=spec-004-test run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  MARKER="$TMP/.planning/run-state/security-fence-spec-004-test.json"
  [ -f "$MARKER" ]
  [ "$(python3 -c "import json;print(json.load(open('$MARKER'))['run_id'])")" = "spec-004-test" ]
  roles="$(python3 -c "import json;print(','.join(json.load(open('$MARKER'))['roles']))")"
  [[ "$roles" == *"gsd-planner"* ]]
  [[ "$roles" == *"gsd-plan-checker"* ]]
}

@test "no trigger (non-security spec) -> no marker written" {
  echo "Phase 1: add pagination to the blog listing page" > "$TMP/.planning/ROADMAP.md"
  GSD_RUN_ID=spec-004-notrigger run bash "$LEVER" "$TMP/.planning"
  [ "$status" -eq 0 ]
  [ ! -f "$TMP/.planning/run-state/security-fence-spec-004-notrigger.json" ]
}

@test "SECURITY_MODEL_FENCE=off with no other config bypasses fail-soft warns too" {
  SECURITY_MODEL_FENCE=off run bash "$LEVER" "$TMP/nonexistent"
  [ "$status" -eq 0 ]
  [[ "$output" == *"kill-switch"* ]]
}

@test "a per-project PLANNING_DIR fences that project's config and stamps the root run-state" {
  # GSD_PROJECT isolation: config lives in .planning/<project>/, but
  # plan-wall.sh reads the fence marker from the root .planning/run-state/.
  mkdir -p "$TMP/.planning/431"
  cp "$TMP/.planning/config.json" "$TMP/.planning/431/config.json"
  echo "Phase 1: harden RLS policies and JWT verification" > "$TMP/.planning/431/ROADMAP.md"
  GSD_RUN_ID=spec-431-test run bash "$LEVER" "$TMP/.planning/431"
  [ "$status" -eq 0 ]
  [ -f "$TMP/.planning/run-state/security-fence-spec-431-test.json" ]
  [ ! -d "$TMP/.planning/431/run-state" ]
  [ "$(python3 -c "import json;print(json.load(open('$TMP/.planning/431/config.json'))['model_overrides']['gsd-planner'])")" = "opus" ]
  # The root project's config is not this run's; it stays untouched.
  [ "$(planner_model gsd-planner)" = "fable" ]
}
